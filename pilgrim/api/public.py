"""The edge-facing routes (RADIO §11; SPLIT_PLAN §1.2).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from pilgrim import dj_contracts as dj
from pilgrim import voting
from pilgrim.api.limits import FreeRequestGate, _make_rate_limiter, _minutes
from pilgrim.api.paid import (
    _LEAK_REASON,
    _leaks_secret,
    _override_secret,
    _station_allowlist,
    add_paid_routes,
)
from pilgrim.config import (
    ROOT,
    Config,
)
from pilgrim.pipelines.request_filter import prefilter

if TYPE_CHECKING:
    from pilgrim.server import PublicSurface, Station

log = logging.getLogger("radio.server")


class RequestIn(BaseModel):
    text: str = Field(..., min_length=1)


async def _public_health(station: Station) -> dict:
    """Public `/api/health`: only what the browser reads.

    No backend probes, no inventory query, no network call (SPLIT_PLAN §1.3).
    `on_air` is the scheduler's committed coverage, which is pure.
    """
    return {"on_air": station.on_air,
            # cached worker state only: no DB read, no FLUX probe (SPLIT_PLAN §1.3)
            "art": {"backend": station.art_worker.backend
                    if station.art_worker else "disabled"},
            "time": time.time()}


_CAT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def cat_manifest(cfg: Config) -> list[str]:
    """The rotating live-cover pool (RADIO §9.1, §11): every image under
    `web_dir/img/cats/`, sorted by name. Globbing the directory means an
    operator adds a file and the next non-song airing shows it."""
    cat_dir = ROOT / cfg.library.web_dir / "img" / "cats"
    names = sorted(p.name for p in cat_dir.glob("*")
                   if p.is_file() and p.suffix.lower() in _CAT_SUFFIXES)
    return [f"img/cats/{n}" for n in names]


def _station_routes(app: FastAPI, station: PublicSurface,
                    health: Callable[[Station], Awaitable[dict]]) -> None:
    """Health, the programme, state, heartbeats and the origin media fallback."""
    cfg = station.cfg

    @app.get("/api/health")
    async def health_endpoint():
        return JSONResponse(await health(station),
                            headers={"Cache-Control": "public, max-age=2"})

    @app.get("/api/station/program")
    async def program(after_seq: int | None = Query(default=None, ge=0),
                      epoch: str | None = Query(default=None, max_length=64),
                      revision: int | None = Query(default=None, ge=0)):
        # docs/shipped/idle.md D2: PLAY is a program fetch with no cursor. Wake first, then
        # build the response, so the waking request already gets a playable
        # programme (thaw only touches rendered inventory: hard rule 1).
        station.wake("program")
        return JSONResponse(station.program_response(after_seq, epoch, revision),
                            headers={"Cache-Control": "public, max-age=1"})

    @app.get("/api/station/state")
    async def station_state():
        """Cheap public station state for DJ-aware clients (RADIO §11, §5.3)."""
        return JSONResponse(station.public_state(),
                            headers={"Cache-Control": "public, max-age=1"})

    @app.get("/api/station/cats")
    async def station_cats():
        """The rotating live-cover pool (RADIO §9.1, §11). Reads the
        filesystem only, never station state: it does not wake a frozen
        station (docs/shipped/idle.md)."""
        return JSONResponse({"cats": cat_manifest(cfg)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dj.HeartbeatIn):
        # item_id comes from the client as media_id (the inventory id); seq is
        # the program position (OVERHAUL 2.5 — they used to be conflated). The
        # revision-aware fields (player_id, observed_epoch/revision, readiness)
        # are accepted and ignored until the browser upgrade lands (docs/systems/console.md §9.2),
        # except player_id, which feeds the live listener count (RADIO §14).
        station.touch_listener(payload.player_id)
        station.record_player_telemetry(payload.player_id, payload)
        station.db.record_airplay(
            item_id=int(payload.media_id or 0),
            seq=int(payload.seq or 0),
            item_type=payload.type,
            started_at=payload.started_at,
            position=payload.position,
            underrun=1 if payload.underrun else 0)
        return {"ok": True}

    @app.get("/api/media/{item_id}")
    async def media(item_id: int):
        it = station.db.get_item(item_id)
        if not it:
            return JSONResponse({"detail": "no such item"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        p = ROOT / it["media_path"]
        if not p.exists():
            return JSONResponse({"detail": "media missing"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        # A media id always maps to the same bytes: every pipeline writes a new
        # file before add_item(); retire keeps it, purge deletes it (SPLIT_PLAN
        # §2 check, no in-place rewrite exists). Safe to cache for a year.
        # Transitional (RADIO §8.4): only items not yet in S3 are fetched here.
        mtype = "audio/mpeg" if p.suffix == ".mp3" else "audio/flac"
        return FileResponse(str(p), media_type=mtype,
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})


def _request_routes(app: FastAPI, station: PublicSurface) -> None:
    """The free request line, the board, request votes and song ratings."""
    cfg = station.cfg
    _rate_limited = _make_rate_limiter(station)
    _free_gate = FreeRequestGate(cfg.requests.free_interval_s)
    _vote_rate_limited = _make_rate_limiter(station, cfg.voting.per_client_per_10min)
    _rating_rate_limited = _make_rate_limiter(station, cfg.ratings.per_client_per_10min)

    def _board() -> dict:
        """The public board, with the voting fields and bury policy applied
        (docs/shipped/voting.md §1/§2). Every handler that returns the board uses this so a
        mixed-version deploy cannot show a row without its score."""
        return station.db.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            stages=station.producer.request_stage,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)

    @app.post("/api/requests")
    async def submit_request(payload: RequestIn, request: Request):
        # One accepted free request per client per interval (RADIO §14),
        # checked before the attempt limit so waiting it out costs no attempts.
        client = (station._client_ip(request)
                  or (request.client.host if request.client else None) or "unknown")
        wait = _free_gate.reserve(client)
        if wait > 0:
            raise HTTPException(
                429,
                f"One free request every {_minutes(cfg.requests.free_interval_s)}. "
                f"Try again in {_minutes(wait)}"
                + (", or jump the line with a priority request."
                   if station.payments_active else "."))
        accepted = False
        try:
            result = await _submit_free(payload, request)
            accepted = bool(result.get("ok"))
            return result
        finally:
            if not accepted:   # refused, rejected or failed: the slot is free again
                _free_gate.release(client)

    async def _submit_free(payload: RequestIn, request: Request) -> dict:
        if _rate_limited(request):
            raise HTTPException(
                429,
                "Easy there — a few requests every ten minutes, please.")
        text = payload.text.strip()
        if not text or len(text) > cfg.requests.max_length:
            raise HTTPException(422, "request empty or too long")
        if _leaks_secret(text, _override_secret(station)):
            # The comp secret pasted into the free line: never stored, logged,
            # moderated or put on the board (docs/shipped/payments.md Step 12b).
            return {"ok": False, "rejected": True, "reason": _LEAK_REASON,
                    "paid_enabled": station.payments_active}
        if station.db.waiting_free_requests() >= cfg.requests.free_queue_max:
            # Free line full (RADIO §14): refuse the new one, never drop a
            # waiting one. Paid requests are never capped.
            log.info("request.line_full",
                     extra={"limit": cfg.requests.free_queue_max})
            board = _board()
            return {"ok": False, "rejected": True, "line_full": True,
                    "reason": "The request line is full right now. Try again in "
                              "a little while, or jump the line with a priority "
                              "request.",
                    "queue": board["queue"], "recent": board["recent"],
                    "cap": cfg.requests.queue_cap,
                    "paid_enabled": station.payments_active}
        t0 = time.monotonic()
        # Deterministic pre-filter first: URLs/contact info never reach the LLM (4.1).
        # The station's own promo codes are exempt (an operator ad may name them).
        pref = prefilter(text, allow=_station_allowlist(station))
        if pref:
            req = station.db.add_request(text, cap=None,
                                         status="rejected", reason=pref)
            log.info("request.received", extra={"request_id": req["id"],
                                                  "len": len(text), "prefilter": True})
            log.info("request.moderated", extra={
                "request_id": req["id"], "allowed": False, "reason": pref,
                "prefilter": True, "duration_ms": 0})
            board = _board()
            return {
                "ok": False, "rejected": True, "request": req, "reason": pref,
                "queue": board["queue"], "recent": board["recent"],
                "cap": cfg.requests.queue_cap,
                "paid_enabled": station.payments_active,
            }
        allowed, reason = await station.moderator.moderate(text)
        status = "queued" if allowed else "rejected"
        req = station.db.add_request(text, cap=None,
                                     status=status, reason=None if allowed else reason)
        log.info("request.received", extra={"request_id": req["id"], "len": len(text)})
        log.info("request.moderated", extra={
            "request_id": req["id"], "allowed": allowed, "reason": reason,
            "prefilter": False,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1)})
        board = _board()
        return {
            "ok": allowed,
            "rejected": not allowed,
            "request": req,
            "reason": None if allowed else reason,
            "queue": board["queue"],
            "recent": board["recent"],
            "cap": cfg.requests.queue_cap,
            "paid_enabled": station.payments_active,
        }

    @app.get("/api/requests")
    async def list_requests():
        board = _board()
        return JSONResponse({**board, "cap": cfg.requests.queue_cap,
                             "paid_enabled": station.payments_active,
                             "voting_enabled": cfg.voting.enabled},
                            headers={"Cache-Control": "public, max-age=2"})

    # ------------------------------------------- community voting (docs/shipped/voting.md §2)
    @app.post("/api/requests/{request_id}/vote")
    async def vote_request(request_id: int, payload: voting.VoteIn,
                           request: Request):
        """+1/-1/0 (retract) on a free request. POST only: the gateway has no
        limit_req zone for PUT/DELETE (docs/shipped/voting.md §0.1)."""
        if not cfg.voting.enabled:
            raise HTTPException(404, "not found")
        if _vote_rate_limited(request):
            raise HTTPException(429, "Easy there — a few votes every ten minutes.")
        status, body = voting.cast_vote(
            station.db, cfg, request_id, payload.voter_id, payload.value,
            station._client_ip(request))
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/station/rating")
    async def rate_song(payload: voting.RatingIn, request: Request):
        """Five turkeys for the on-air song. The window is server-authoritative
        (docs/shipped/voting.md V8); the client only hides the control."""
        if not cfg.ratings.enabled:
            raise HTTPException(404, "not found")
        if _rating_rate_limited(request):
            raise HTTPException(429, "Easy there — a few ratings every ten minutes.")
        status, body = voting.cast_rating(
            station.db, cfg, station.scheduler, payload,
            station._client_ip(request))
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})


def _visitor_routes(app: FastAPI, station: PublicSurface) -> None:
    """The visitor counter."""
    @app.post("/api/visitors")
    async def register_visitor(request: Request):
        """Register the server-derived client IP and return the unique count
        (RADIO §14). Idempotent per client; media fetches / heartbeats / health
        polls / request submissions never hit this. Always 200 with a count.
        Also returns the DB request counts the HIT COUNTER pane shows
        (RADIO §9.1): `paid_requests` (paid priority rows) and
        `total_requests` (every request row)."""
        n = station.register_visitor(request)
        resp = JSONResponse({"unique_visitors": n, **station.request_counts()},
                            headers={"Cache-Control": "no-store"})
        return resp


def add_public_routes(app: FastAPI, station: PublicSurface,
                      health: Callable[[Station], Awaitable[dict]]) -> None:
    """Register the edge-facing surface exactly once, shared by both apps.

    Only these routes exist on the public listener (SPLIT_PLAN §1.2). The
    admin app calls this too, passing the full health handler; admin-only
    routes are added separately by `add_admin_routes`.
    """
    _station_routes(app, station, health)
    _request_routes(app, station)
    add_paid_routes(app, station)
    _visitor_routes(app, station)
