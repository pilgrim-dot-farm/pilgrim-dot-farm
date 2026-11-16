"""Paid priority requests: checks, summaries and routes (RADIO §11; docs/shipped/payments.md).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from pilgrim import voting
from pilgrim.api.limits import _make_rate_limiter
from pilgrim.payments import (
    StripeError,
    WebhookError,
    verify_webhook,
)
from pilgrim.pipelines.request_filter import prefilter
from pilgrim.pipelines.request_parts import RequestParts, is_label_line
from pilgrim.pipelines.request_parts import from_row as parts_from_row
from pilgrim.pipelines.request_parts import normalise as normalise_paid

if TYPE_CHECKING:
    from pilgrim.server import PublicSurface, Station

log = logging.getLogger("radio.server")


PAID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


_SECTION_TAG = re.compile(r"^\[[^\]]{1,40}\]$")


# Minimum length for the comp secret: a short value could be guessed through
# the public form, so anything shorter leaves the feature off.
_MIN_OVERRIDE_LEN = 24


_CHECK_FAILED = ("Couldn't finish checking your request. You were not charged; "
                 "try again.")


_LEAK_REASON = "That request can't be accepted."


class PaidRequestIn(BaseModel):
    """`{lyrics, music, art}` (docs/shipped/prompt-upgrade.md) or the legacy `{text}`.
    `promo` is a promo code (or the operator comp token) from its own UI box
    (2026-11-08); `voter_id` is the per-browser identity used to stop the same
    listener redeeming one promo code twice."""
    text: str | None = None
    lyrics: str | None = None
    music: str | None = None
    art: str | None = None
    promo: str | None = None
    voter_id: str | None = Field(None, pattern=voting.VOTER_PATTERN)

    @model_validator(mode="after")
    def _one_form(self) -> PaidRequestIn:
        three = any(v is not None for v in (self.lyrics, self.music, self.art))
        if three and self.text:
            raise ValueError("send lyrics/music/art or text, not both")
        if not three and not (self.text or "").strip():
            raise ValueError("text required")
        return self


class PromoIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    voter_id: str | None = Field(None, pattern=voting.VOTER_PATTERN)


def _fallback_summary(text: str, max_length: int) -> str:
    """First non-tag line, collapsed; never a bare [verse]/[intro] marker or
    a composed-parts label (`Lyrics:` etc.)."""
    for line in text.split("\n"):
        clean = " ".join(line.split())
        if not clean or _SECTION_TAG.match(clean) or is_label_line(clean):
            continue
        if len(clean) > max_length:
            clean = clean[: max_length - 1] + "…"
        return clean
    clean = " ".join(text.split())
    return clean[: max_length - 1] + "…" if len(clean) > max_length else clean


async def _summarise_paid(station: PublicSurface, text: str, max_length: int,
                          fallback_text: str | None = None) -> str:
    """One-line board label for a paid request (docs/shipped/payments.md §3.4). LLM first,
    deterministic fallback when the model fails; never blocks the request.

    docs/shipped/prompt-upgrade.md Step 2: the LLM sees lyrics + music only (never art), and
    the fallback label comes from the subject text."""
    fallback = _fallback_summary(fallback_text if fallback_text is not None
                                 else text, max_length)
    template = station.prompts.get("paid_summary")
    if not template:
        return fallback
    started = time.monotonic()
    try:
        raw = await station.llm.chat_text(
            station.cfg.models.briefs, template, json.dumps(text))
    except Exception as exc:  # noqa: BLE001 - a label must never fail the request
        log.warning("payment.summary_failed",
                    extra={"error": type(exc).__name__})
        return fallback
    summary = " ".join(raw.split()).strip().strip('"').strip("'")
    summary = summary.lstrip("*").strip()
    if not summary:
        # An empty answer used to fall back silently (a 512-token cap starved
        # the reasoning model, Oct 31); say so.
        log.warning("payment.summary_empty")
        return fallback
    if len(summary) > max_length:
        summary = summary[: max_length - 1] + "…"
    log.info("payment.summarised",
             extra={"len": len(summary),
                    "duration_ms": round((time.monotonic() - started) * 1000, 1)})
    return summary


async def _backfill_paid_summaries(station: Station) -> None:
    """Regenerate board labels for paid requests on the board (waiting or
    aired) whose label is missing or just the raw-text fallback (left by the
    old 512-token cap)."""
    max_length = station.cfg.requests.max_length
    for row in station.db.paid_rows_needing_summary():
        text = str(row.get("text") or "")
        parts = parts_from_row(row)
        song_text = parts.song_text() if parts else text
        subject = parts.subject_text() if parts else text
        fallback = _fallback_summary(subject, max_length)
        if row.get("summary") and row["summary"] != fallback:
            continue
        summary = await _summarise_paid(station, song_text, max_length,
                                        fallback_text=subject)
        if summary != row.get("summary"):
            station.db.set_request_summary(int(row["id"]), summary)
            log.info("payment.summary_backfilled", extra={"request_id": row["id"]})


def _normalise_paid(text: str) -> str:
    """docs/shipped/payments.md Step 4.2: CRLF -> LF, rstrip lines, strip, 3+ blank -> 2.
    The one implementation lives in pipelines/request_parts.py."""
    return normalise_paid(text)


def _override_secret(station: PublicSurface) -> str:
    secret = station.override_token or ""
    return secret if len(secret) >= _MIN_OVERRIDE_LEN else ""


def _split_override(text: str, secret: str) -> tuple[bool, str]:
    """(comped, prompt): the first line equal to the secret (constant-time)
    marks an operator comp and is removed before anything else sees the text."""
    if not secret:
        return False, text
    first, _, rest = text.partition("\n")
    if hmac.compare_digest(first.strip().encode(), secret.encode()):
        return True, _normalise_paid(rest)
    return False, text


def _leaks_secret(text: str, secret: str) -> bool:
    return bool(secret) and secret in text


def _promo_identity(station: PublicSurface, request: Request,
                    voter_id: str | None) -> str:
    """Stable identity for one promo redemption. The browser's voter id is
    the primary identity; without one the canonical client IP stands in."""
    if voter_id and voting.VOTER_RE.match(voter_id):
        return "voter:" + voter_id
    ip = (station._client_ip(request)
          or (request.client.host if request.client else None) or "unknown")
    return "ip:" + ip


def _release_promo(station: PublicSurface, promo_code: str | None,
                   promo_identity: str | None) -> None:
    """Return a promo use when the request it comped never reached the line."""
    if promo_code and promo_identity:
        station.db.release_promo_redemption(promo_code, promo_identity)


def _station_allowlist(station: PublicSurface) -> tuple[str, ...]:
    """The station's own issued promo codes are its identity, not listener
    contact info: an operator ad may name them on the request line
    (OVERHAUL 4.1), so the pre-filter masks them before the contact checks.
    A store error degrades to no allowlist — the filter stays strict."""
    try:
        return tuple(c["code"] for c in station.db.list_promo_codes())
    except Exception:
        return ()


async def _run_paid_checks(station: PublicSurface, request_id: int, text: str,
                           comped: bool, parts: RequestParts | None = None,
                           promo_code: str | None = None,
                           promo_identity: str | None = None) -> None:
    """Moderation -> copyright screen -> summary -> ready (or comped into the
    queue). Runs after the POST has answered; the client polls the stage
    (docs/shipped/payments.md Step 12b). Any crash rejects the row: nobody is charged.

    docs/shipped/prompt-upgrade.md Step 2 (D4): the moderator sees the composed text; the
    copied-lyrics screen and the board summary never see the art part.

    A promo code's use is spent at submit (atomically); if moderation or the
    copyright screen rejects the request, the use is returned so a rejected
    request never burns a code."""
    db = station.db
    cfg = station.cfg
    song_text = parts.song_text() if parts else text
    subject = parts.subject_text() if parts else text
    try:
        t0 = time.monotonic()
        allowed, reason = await station.moderator.moderate(text)
        log.info("request.moderated", extra={
            "request_id": request_id, "allowed": allowed, "reason": reason,
            "prefilter": False, "paid": True,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1)})
        if not allowed:
            db.reject_checking_request(request_id, reason or "rejected")
            _release_promo(station, promo_code, promo_identity)
            return
        if not db.set_request_stage(request_id, "copyright"):
            _release_promo(station, promo_code, promo_identity)
            return
        verdict = await station.copyright.check(song_text, request_id)
        if not verdict.allowed:
            db.reject_checking_request(request_id, verdict.reason)
            _release_promo(station, promo_code, promo_identity)
            return
        summary = await _summarise_paid(station, song_text,
                                        cfg.requests.max_length,
                                        fallback_text=subject)
        if comped:
            db.set_request_stage(request_id, "ready", summary=summary)
            if db.mark_request_comped(request_id):
                log.info("payment.comped", extra={"request_id": request_id})
            return
        db.set_request_stage(request_id, "ready", summary=summary)
    except Exception as exc:  # noqa: BLE001 - a failed check must never charge
        log.warning("payment.check_failed",
                    extra={"request_id": request_id, "error": type(exc).__name__})
        db.reject_checking_request(request_id, _CHECK_FAILED)
        _release_promo(station, promo_code, promo_identity)


def add_paid_routes(app: FastAPI, station: PublicSurface) -> None:
    """Paid priority requests, promo codes and the Stripe webhook (RADIO §11)."""
    cfg = station.cfg
    _paid_rate_limited = _make_rate_limiter(station, cfg.payments.per_client_per_10min)

    # ------------------------------------------------- paid priority (PAYMENT §4)
    @app.post("/api/paid-requests")
    async def submit_paid_request(payload: PaidRequestIn, request: Request,
                                  background: BackgroundTasks):
        """docs/shipped/payments.md Step 12b: prefilter now, then moderation + copyright in
        the background; the client polls the stage and asks for Checkout
        only once the row is `ready`.

        docs/shipped/prompt-upgrade.md Step 2: `{lyrics, music, art}` (both lyrics and
        music required) or the legacy `{text}`.
        """
        if _paid_rate_limited(request):
            raise HTTPException(
                429, "Easy there — a few paid requests every ten minutes, please.")
        secret = _override_secret(station)
        three = any(v is not None for v in (payload.lyrics, payload.music,
                                            payload.art))
        parts: RequestParts | None
        if three:
            parts = RequestParts(
                normalise_paid(payload.lyrics or ""),
                normalise_paid(payload.music or ""),
                normalise_paid(payload.art or ""))
            text = parts.composed()
        else:
            parts = None
            text = _normalise_paid(payload.text or "")
        # Operator comp (D7): the promo box (validated on APPLY and again
        # here) or the legacy first line of the lyrics box / text. A promo
        # code spends one use here, atomically; an operator token is unlimited.
        promo_ok = False
        promo_code = None
        promo_identity = None
        if payload.promo:
            code = payload.promo.strip()
            if secret and hmac.compare_digest(code.encode(), secret.encode()):
                promo_ok = True
            else:
                promo_code = code
                promo_identity = _promo_identity(station, request,
                                                 payload.voter_id)
                redeemed = station.db.redeem_promo_code(promo_code,
                                                        promo_identity)
                if not redeemed["ok"]:
                    status = 403 if redeemed["code"] == "invalid" else 409
                    return JSONResponse(status_code=status, content={
                        "ok": False, "code": redeemed["code"],
                        "detail": redeemed["detail"]})
                promo_ok = True
        comp_source = parts.lyrics if parts else text
        comped, stripped = _split_override(comp_source, secret)
        comped = comped or promo_ok
        if comped and parts is not None:
            parts = RequestParts(stripped, parts.music, parts.art)
            text = parts.composed()
        elif comped:
            text = stripped
        if not comped and (not station.payments_active
                           or station.payment_service is None):
            return JSONResponse(status_code=503, content={
                "detail": "Paid requests are closed right now.",
                "paid_closed": True})
        limits = cfg.payments.parts
        if parts is not None:
            if not parts.has_song:
                _release_promo(station, promo_code, promo_identity)
                raise HTTPException(
                    422, "both lyrics and musical direction are required")
            for name, value, cap in (("Lyrics", parts.lyrics, limits.lyrics_max),
                                     ("Musical direction", parts.music,
                                      limits.music_max),
                                     ("Cover art", parts.art, limits.art_max)):
                if len(value) > cap:
                    _release_promo(station, promo_code, promo_identity)
                    raise HTTPException(
                        422, f"{name} is too long ({cap} characters max)")
            text = parts.composed()
        lines = text.count("\n") + 1
        if (not text or len(text) > cfg.payments.paid_max_length
                or lines > cfg.payments.paid_max_lines):
            _release_promo(station, promo_code, promo_identity)
            raise HTTPException(422, "paid request empty or too long")
        if _leaks_secret(text, secret):
            _release_promo(station, promo_code, promo_identity)
            return {"ok": False, "rejected": True, "reason": _LEAK_REASON}
        pref = prefilter(text, phone_mode="runs",
                         allow=_station_allowlist(station))
        if pref:
            _release_promo(station, promo_code, promo_identity)
            req = station.db.add_request(text, cap=None, status="rejected",
                                         reason=pref, priority=1)
            log.info("request.moderated", extra={
                "request_id": req["id"], "allowed": False, "reason": pref,
                "prefilter": True, "paid": True, "duration_ms": 0})
            return {"ok": False, "rejected": True, "reason": pref}
        row = station.db.add_checking_request(text, comped=comped, parts=parts)
        log.info("payment.requested",
                 extra={"request_id": row["id"], "len": len(text), "lines": lines,
                        "comped": comped, "parts": parts is not None,
                        "promo": promo_code is not None,
                        "len_lyrics": len(parts.lyrics) if parts else 0,
                        "len_music": len(parts.music) if parts else 0,
                        "len_art": len(parts.art) if parts else 0,
                        "client_ip": station._client_ip(request) if comped else None})
        background.add_task(_run_paid_checks, station, int(row["id"]), text,
                            comped, parts, promo_code, promo_identity)
        return {"ok": True, "token": row["public_token"], "stage": "moderation"}

    @app.post("/api/paid-requests/promo")
    async def validate_promo(payload: PromoIn, request: Request):
        """UI_REFACTOR feedback (2026-11-08): the promo code has its own box
        with an APPLY button. Validate the operator comp token in constant
        time, or check a stored promo code's remaining uses and this
        identity's redemption, without echoing the code. The client counts
        its own attempts; submit re-checks and spends a use atomically."""
        secret = _override_secret(station)
        code = payload.code.strip()
        if secret and hmac.compare_digest(code.encode(), secret.encode()):
            return {"ok": True}
        identity = _promo_identity(station, request, payload.voter_id)
        res = station.db.promo_code_status(code, identity)
        if res["ok"]:
            return {"ok": True}
        return {"ok": False, "code": res["code"], "detail": res["detail"]}

    @app.post("/api/paid-requests/{token}/checkout")
    async def start_paid_checkout(token: str):
        """Create the Checkout Session for a row that passed every check. The
        client_secret is returned once and never stored (AGENTS §1.11)."""
        if not PAID_TOKEN_RE.match(token):
            raise HTTPException(404, "not found")
        if not station.payments_active or station.payment_service is None:
            return JSONResponse(status_code=503, content={
                "detail": "Paid requests are closed right now.",
                "paid_closed": True})
        row = station.db.get_request_by_token(token)
        if row is None:
            raise HTTPException(404, "not found")
        if not station.db.claim_for_checkout(int(row["id"])):
            return JSONResponse(status_code=409, content={
                "detail": "That request isn't ready for payment."})
        try:
            session = await station.payment_service.start_checkout(row)
        except StripeError as exc:
            station.db.mark_request_abandoned(int(row["id"]))
            log.warning("payment.checkout_failed",
                        extra={"request_id": row["id"], "kind": exc.kind})
            return JSONResponse(status_code=424, content={
                "detail": "Payments are having a moment. You were not charged."})
        if not session.client_secret:
            station.db.mark_request_abandoned(int(row["id"]))
            log.warning("payment.checkout_no_secret",
                        extra={"request_id": row["id"], "session_id": session.id})
            return JSONResponse(status_code=424, content={
                "detail": "Payments are having a moment. You were not charged."})
        return JSONResponse(content={
            "ok": True,
            "client_secret": session.client_secret,
            "publishable_key": station.publishable_key,
            "expires_at": session.expires_at
            or int(station.clock.wall() + cfg.payments.checkout_expiry_s),
        })

    # ------------------------------------------- paid status + reconcile (§6)
    @app.get("/api/paid-requests/{token}")
    async def paid_request_status(token: str):
        if not PAID_TOKEN_RE.match(token):
            raise HTTPException(404, "not found")
        if station.payment_service is not None:
            row = await station.payment_service.reconcile(token)
        else:
            row = station.db.get_request_by_token(token)
        if row is None:
            raise HTTPException(404, "not found")
        status = str(row["status"])
        position = (station.db.request_position(int(row["id"]))
                    if status in ("queued", "producing", "ready") else None)
        body: dict = {"status": status, "position": position,
                      "stage": row.get("stage") if status == "checking" else None,
                      "comped": bool(row.get("comped"))}
        if status == "rejected":
            body["reason"] = row.get("reason")
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------- Stripe webhook (§5)
    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        if station.payment_service is None:
            return JSONResponse(status_code=503, content={"detail": "payments disabled"})
        payload = await request.body()
        if len(payload) > 256 * 1024:
            return JSONResponse(status_code=413, content={"detail": "payload too large"})
        try:
            event = verify_webhook(
                payload,
                request.headers.get("stripe-signature", ""),
                station.webhook_secret,
                station.clock.wall(),
                cfg.payments.webhook_tolerance_s,
            )
        except WebhookError as exc:
            log.warning("payment.webhook_rejected", extra={"reason": str(exc)})
            return JSONResponse(status_code=400, content={"detail": "bad signature"})
        outcome = station.payment_service.handle_event(event)
        return JSONResponse({"received": True, "outcome": outcome})
