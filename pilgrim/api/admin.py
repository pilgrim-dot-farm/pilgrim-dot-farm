"""The operator routes: station control, DJ console, audits, chat (RADIO §11).
"""
from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from pilgrim import dj_contracts as dj
from pilgrim.chat import identity
from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import AuditLog
from pilgrim.chat.redis_store import ChatStore
from pilgrim.chat.redis_store import connect as chat_connect
from pilgrim.config import (
    load_secret,
)
from pilgrim.dj_security import require_dj
from pilgrim.hotstate import REDIS_URL_ENV
from pilgrim.pipelines.request_parts import RequestParts
from pilgrim.pipelines.request_parts import from_row as parts_from_row

if TYPE_CHECKING:
    from pilgrim.server import Station

log = logging.getLogger("radio.server")


class BenchItemIn(BaseModel):
    """One benchmark request (docs/systems/queue-bench.md)."""
    text: str | None = Field(default=None, max_length=4000)
    paid: bool = False
    votes: int = Field(default=0, ge=-20, le=20)   # synthetic up/down votes (free only)
    replay_of: int | None = None                   # reuse a past request's text
    reuse_lyrics: bool = True                      # ...and the words its song was made from
    short: bool = False                            # render bench.short_duration_s, not full length


class BenchIn(BaseModel):
    run: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")
    requests: list[BenchItemIn] = Field(min_length=1)


async def _admin_health(station: Station) -> dict:
    """Full health for the admin listener (unchanged from today)."""
    return await station.health()


class PlayNextIn(BaseModel):
    """Operator "play this song next" (POST /api/admin/dj/play).

    Deliberately not the full `dj.QueueRequest` command contract (command_id /
    expected_epoch / expected_revision): this route is the narrow operator
    convenience, not the scheduler command service."""

    media_id: int


def _control_routes(app: FastAPI, station: Station) -> None:
    """Station start/stop, voices, and play-this-next."""
    cfg = station.cfg

    @app.post("/api/station/start")
    async def start():
        return {"ok": True, "message": "station is always on air"}

    @app.post("/api/station/stop")
    async def stop():
        return {"ok": True, "message": "stop is a no-op in Phase 1 (station stays on air)"}

    @app.get("/api/admin/voices")
    async def admin_voices():
        with contextlib.suppress(Exception):
            return {"voices": await station.kokoro.voices()}
        return {"voices": []}

    # ---- DJ console (RADIO §11): read-only behind the operator token. Control
    # mutations (skip/queue/phrases) land with the scheduler command service and
    # additionally require dj.enabled (docs/systems/console.md §7). ----
    @app.post("/api/admin/dj/play", dependencies=[Depends(require_dj)])
    async def dj_play(payload: PlayNextIn):
        """Operator: air this song next (ALBUM_ART demo / TUI §5).

        A narrow control mutation: it splices a *ready, airable song* into the
        slot after whatever is playing, with the words-between-songs bridges,
        and bumps the revision so clients refetch. It does not create a
        request, does not touch payments, and never waits on production.
        """
        if not cfg.dj.enabled:
            raise HTTPException(403, detail={
                "code": "dj_disabled", "detail": "DJ control is disabled"})
        # docs/shipped/idle.md D2: an operator mutation is a human presence; wake first so
        # the splice lands in a running programme, not a frozen window.
        station.wake("dj")
        seq = station.scheduler.place_song_next(int(payload.media_id))
        if seq is None:
            raise HTTPException(409, detail={
                "code": "cannot_place",
                "detail": "not an airable song, or no legal slot this cycle"})
        return {"ok": True, "seq": seq,
                "revision": station.scheduler.revision}


def _queue_routes(app: FastAPI, station: Station) -> None:
    """Vote audit and the queue benchmark."""
    cfg = station.cfg

    @app.get("/api/admin/voting", dependencies=[Depends(require_dj)])
    async def admin_voting(limit: int = Query(default=100, ge=1, le=500)):
        """Audit only (docs/shipped/voting.md §2): recent votes and ratings, plus the
        currently buried free rows. Never exposes voter ids or IPs."""
        threshold = cfg.voting.bury_threshold
        return {"votes": station.db.recent_votes(limit),
                "ratings": station.db.recent_ratings(limit),
                "buried": (station.db.buried_requests(threshold, limit)
                           if threshold is not None else [])}

    # ---- Queue benchmark (docs/systems/queue-bench.md, RADIO §11). Admin
    # listener only. Bench rows use the real queue, brief LLM and render pool;
    # their audio is thrown away and they are never on the public board.
    @app.post("/api/admin/bench", dependencies=[Depends(require_dj)])
    async def bench_add(body: BenchIn):
        if not cfg.bench.enabled:
            raise HTTPException(status_code=404, detail="bench is disabled")
        if len(body.requests) > cfg.bench.max_batch:
            raise HTTPException(status_code=422, detail={
                "code": "too_many", "max_batch": cfg.bench.max_batch})
        # resolve every row first: a bad one must not leave half a batch queued
        resolved: list[tuple[BenchItemIn, str, RequestParts | None]] = []
        for i, item in enumerate(body.requests):
            text, parts = (item.text or "").strip(), None
            if item.replay_of is not None:
                src = station.db.get_request(item.replay_of)
                if src is None:
                    raise HTTPException(status_code=422, detail={
                        "code": "replay_not_found", "index": i, "replay_of": item.replay_of})
                text = text or str(src["text"])
                parts = parts_from_row(src) if item.paid else None
            if not text:
                raise HTTPException(status_code=422, detail={
                    "code": "empty", "index": i})
            resolved.append((item, text, parts))
        # no await below: the producer shares this loop, so it sees the whole
        # batch with its votes at once, never a half-voted queue
        out = []
        for i, (item, text, parts) in enumerate(resolved):
            opts: dict[str, Any] = {"run": body.run, "index": i}
            if item.short:
                opts["duration_s"] = cfg.bench.short_duration_s
            if item.replay_of is not None:
                opts.update(replay_of=item.replay_of, reuse_lyrics=item.reuse_lyrics)
            row = station.db.add_bench_request(text, paid=item.paid, opts=opts, parts=parts)
            if not item.paid:  # paid rows are not votable (docs/shipped/voting.md V4)
                for v in range(abs(item.votes)):
                    station.db.cast_request_vote(
                        int(row["id"]), f"bench-{body.run}-{i}-{v}", None,
                        1 if item.votes > 0 else -1,
                        max_voters_per_ip=cfg.voting.max_voters_per_ip)
            out.append({"id": row["id"], "paid": item.paid, "votes": item.votes})
        log.info("bench.added", extra={"run": body.run, "count": len(out)})
        return {"run": body.run, "requests": out}

    @app.get("/api/admin/bench", dependencies=[Depends(require_dj)])
    async def bench_list(run: str | None = Query(default=None, max_length=60),
                         candidates: int = Query(default=0, ge=0, le=200)):
        """Benchmark rows with their timings, oldest first (the report tool
        reads this). `stage` is the producer's live stage for a row in flight;
        `candidates=N` adds the N newest aired listener requests to replay."""
        rows = []
        for r in station.db.bench_requests():
            try:
                opts = json.loads(r.get("bench_opts") or "{}")
            except ValueError:
                opts = {}
            if run is not None and opts.get("run") != run:
                continue
            rows.append({"id": r["id"], "status": r["status"], "paid": bool(r["priority"]),
                         "score": int(r.get("score") or 0), "attempts": r.get("attempts") or 0,
                         "created_at": r["created_at"], "claimed_at": r.get("claimed_at"),
                         "updated_at": r.get("updated_at"),
                         "stage": station.producer.request_stage.get(int(r["id"])),
                         "text": str(r["text"])[:120], "opts": opts})
        return {"requests": rows,
                "candidates": station.db.replay_candidates(candidates) if candidates else [],
                "limits": {"briefs": cfg.models.max_concurrency.get(cfg.models.briefs, 1),
                           "render_slots": station.producer._capacity(),
                           "free_request_slots": cfg.songs.free_request_slots}}

    @app.delete("/api/admin/bench", dependencies=[Depends(require_dj)])
    async def bench_purge(apply: bool = Query(default=False)):
        """Delete benchmark rows and their votes (dry run unless apply=true).
        Queued rows are deleted too, which cancels the rest of a run."""
        return {"applied": apply, **station.db.purge_bench_requests(apply=apply)}


def _dj_routes(app: FastAPI, station: Station) -> None:
    """The DJ console: state, catalogue, character, phrase jobs."""
    cfg = station.cfg

    @app.get("/api/admin/dj/state", response_model=dj.DjState,
             dependencies=[Depends(require_dj)])
    async def dj_state():
        """Read-only console state; no backend probes (docs/systems/console.md §4)."""
        return station.dj_state()

    @app.get("/api/admin/dj/songs", response_model=dj.DjSongPage,
             dependencies=[Depends(require_dj)])
    async def dj_songs(q: str | None = Query(default=None, max_length=200),
                       sort: dj.SortField = "title",
                       direction: dj.SortDirection = "asc",
                       cursor: str | None = Query(default=None, max_length=512),
                       limit: int = Query(default=100, ge=1, le=500)):
        """Sortable/searchable song catalogue (docs/systems/console.md §2, §4)."""
        return station.dj_songs(q, sort, direction, cursor, limit)

    @app.get("/api/admin/dj/character", response_model=dj.CharacterInfo,
             dependencies=[Depends(require_dj)])
    async def dj_character():
        """Operator character + setup readiness + unused voices (docs/systems/console.md §6).
        The voice list is the one live probe here; failure is surfaced as an
        explicit error, never a silent empty pick."""
        ch = cfg.dj.character
        # The exclusion set is every role voice *plus the character's own
        # reservation*: an assigned voice is not an "available unused" one
        # (RADIO.md §10). Empty strings never match a real voice id.
        used = {cfg.voices.dj, cfg.voices.news, cfg.voices.commercials,
                cfg.voices.liners, cfg.voices.field_reporter}
        if ch.voice:
            used.add(ch.voice)
        available: list = []
        error: str | None = None
        try:
            voices = await station.kokoro.voices()
            # Kokoro /voices is `{voices: [...], default: ...}` (probe, §2);
            # tolerate a bare list too.
            if isinstance(voices, dict):
                voices = voices.get("voices") or []
            available = [v for v in voices if v not in used]
        except Exception as e:  # noqa: BLE001 - surfaced to the operator untouched
            error = str(e)
        return {
            "character_id": ch.id,
            "name": ch.name,
            "setup_complete": bool(ch.name and ch.voice),
            "voice": ch.voice or None,
            "available_voices": available,
            "voice_discovery_error": error,
            "block_reason": None if bool(ch.name and ch.voice)
            else "character name/voice not both set",
        }

    @app.post("/api/admin/dj/phrases/validate", response_model=dj.ValidateResponse,
              dependencies=[Depends(require_dj)])
    async def phrases_validate(payload: dj.PhraseValidateRequest):
        """Pure cleaned-text validation; no synthesis or scheduling (docs/systems/console.md §6)."""
        return station.phrases.validate(payload.character_id, payload.text)

    @app.post("/api/admin/dj/phrases", response_model=dj.PhraseSubmitResponse,
              dependencies=[Depends(require_dj)], status_code=202)
    async def phrases_submit(payload: dj.PhraseSubmitRequest):
        """Validate the confirmed text, persist a job, render it in the
        background and place it at the earliest legal boundary. Idempotent by
        command_id (identical retry returns the existing job; different content
        with the same command_id is 409)."""
        try:
            return station.phrases.submit(
                payload.command_id, payload.character_id,
                payload.text, payload.cleaned_text_hash)
        except ValueError as e:
            code = str(e)
            if code in ("dj_disabled",):
                raise HTTPException(403, detail={"code": code,
                    "detail": "DJ controls are not enabled yet"}) from None
            if code in ("conflict", "hash_mismatch", "no_voice"):
                raise HTTPException(409, detail={"code": code,
                    "detail": "phrase conflicts with prior submission"}) from None
            raise HTTPException(422, detail={"code": code,
                "detail": "phrase rejected"}) from None

    @app.get("/api/admin/dj/phrases/{job_id}", response_model=dj.PhraseJob,
             dependencies=[Depends(require_dj)])
    async def phrases_job(job_id: int):
        """Phrase job render stage and finished clip (docs/systems/console.md §6)."""
        job = station.phrases.get(job_id)
        if not job:
            raise HTTPException(404, detail={"code": "no_such_job",
                "detail": "no phrase job with that id"})
        return job


def _payment_routes(app: FastAPI, station: Station) -> None:
    """Paid-request audit."""
    @app.get("/api/admin/payments", dependencies=[Depends(require_dj)])
    async def admin_payments(
        status: str = Query(default="failed,awaiting_payment,abandoned,queued"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """Paid request audit + money-without-a-request events (docs/shipped/payments.md §9)."""
        statuses = [s for s in status.split(",") if s]
        keys = ("id", "status", "priority", "created_at", "paid_at",
                "amount_cents", "currency", "payment_intent_id",
                "checkout_session_id", "attempts", "text")
        return {
            "requests": [{k: row.get(k) for k in keys}
                         for row in station.db.paid_requests(statuses, limit)],
            "mismatches": station.db.mismatch_events(50),
        }


def _chat_routes(app: FastAPI, station: Station) -> None:
    """Sheep Pen moderation controls."""
    cfg = station.cfg

    # ---- The Sheep Pen (docs/shipped/sheep-pen.md §0.5.2, §10). These routes exist only on
    # the Mac's loopback admin listener, behind require_dj. They act by writing
    # to Valkey (events stream + PUBLISH), which every Fargate task observes.
    # The web tier has no admin routes (_add_api_404 answers 404). ----
    class ChatDeleteIn(BaseModel):
        message_id: str = Field(min_length=3, max_length=64)
        reason: str | None = Field(default=None, max_length=200)

    class ChatBanIn(BaseModel):
        scope: str = Field(pattern="^(user|ip)$")
        target: str = Field(min_length=1, max_length=64)
        seconds: int | None = Field(default=None, ge=1)
        reason: str = Field(default="operator ban", max_length=200)

    class ChatUnbanIn(BaseModel):
        scope: str = Field(pattern="^(user|ip)$")
        target: str = Field(min_length=1, max_length=64)

    class ChatNameIn(BaseModel):
        user_id: str = Field(min_length=1, max_length=64)
        name: str | None = Field(default=None, max_length=80)

    class ChatPostingIn(BaseModel):
        enabled: bool
        ttl_s: int | None = Field(default=None, ge=1)

    class ChatSlowIn(BaseModel):
        interval_s: float | None = Field(default=None, gt=0)

    _chat: dict[str, Any] = {"cache": None, "audit": None, "actions": None}

    async def _chat_actions() -> ChatActions:
        if not cfg.chat.enabled:
            raise HTTPException(404, detail={"code": "chat_disabled",
                                             "detail": "chat is disabled"})
        if _chat["cache"] is None:
            url = load_secret(REDIS_URL_ENV)
            if not url:
                raise HTTPException(503, detail={"code": "chat_unavailable",
                    "detail": f"{REDIS_URL_ENV} is not set"})
            cache = chat_connect(url, cfg)
            _chat["cache"] = cache
            _chat["audit"] = AuditLog(cfg)
            _chat["actions"] = ChatActions(cfg, station.db, cache,
                                           _chat["audit"], station.clock)
        return _chat["actions"]

    @app.get("/api/admin/chat/state", dependencies=[Depends(require_dj)])
    async def admin_chat_state():
        """Backlog, cursor, switches and recent decisions (docs/shipped/sheep-pen.md §10)."""
        await _chat_actions()
        cache: ChatStore = _chat["cache"]
        try:
            cursor = await cache.get("moderation:cursor")
            heartbeat = await cache.get("worker:heartbeat")
            slow = await cache.get("slow_mode")
            disabled = await cache.exists("posting_disabled")
            length = await cache.xlen()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, detail={"code": "chat_unavailable",
                "detail": f"Valkey unreachable: {type(e).__name__}"}) from None
        return {
            "cursor": cursor,
            "stream_len": length,
            "worker_heartbeat": heartbeat,
            "posting_disabled": disabled,
            "slow_mode": float(slow) if slow else None,
            "pending_review": len(station.db.pending_chat_messages(500)),
            "recent_decisions": station.db.recent_chat_moderation(50),
        }

    @app.post("/api/admin/chat/delete", dependencies=[Depends(require_dj)])
    async def admin_chat_delete(payload: ChatDeleteIn):
        actions = await _chat_actions()
        ok = await actions.delete_message(payload.message_id, actor="operator",
                                          reason=payload.reason)
        return {"ok": ok}

    @app.post("/api/admin/chat/ban", dependencies=[Depends(require_dj)])
    async def admin_chat_ban(payload: ChatBanIn):
        actions = await _chat_actions()
        if payload.scope == "user":
            until = await actions.ban_user(payload.target, payload.reason,
                                           actor="operator",
                                           until=(station.clock.wall() + payload.seconds
                                                  if payload.seconds else None))
        else:
            until = await actions.ban_ip(payload.target, payload.reason,
                                         actor="operator", seconds=payload.seconds)
        return {"ok": True, "until": until}

    @app.post("/api/admin/chat/unban", dependencies=[Depends(require_dj)])
    async def admin_chat_unban(payload: ChatUnbanIn):
        actions = await _chat_actions()
        return {"ok": True, "cleared": await actions.unban(payload.scope,
                                                            payload.target,
                                                            actor="operator")}

    @app.post("/api/admin/chat/name", dependencies=[Depends(require_dj)])
    async def admin_chat_name(payload: ChatNameIn):
        """Rename a user (keeps the user id) or release their name."""
        await _chat_actions()
        cache: ChatStore = _chat["cache"]
        user = station.db.chat_user(payload.user_id)
        if user is None:
            raise HTTPException(404, detail={"code": "no_such_user",
                "detail": "no chat user with that id"})
        if not payload.name:
            await identity.release_name(station.db, cache, payload.user_id,
                                        user.get("name_norm"),
                                        epoch=station.clock.wall())
            return {"ok": True, "released": True}
        try:
            updated = await identity.claim_name(station.db, cache, cfg, user,
                                                payload.name,
                                                epoch=station.clock.wall(),
                                                allow_change=True)
        except identity.NameError_ as e:
            status = 409 if e.code == "name_taken" else 400
            raise HTTPException(status, detail={"code": e.code}) from None
        return {"ok": True, "name": updated.get("name")}

    @app.post("/api/admin/chat/posting", dependencies=[Depends(require_dj)])
    async def admin_chat_posting(payload: ChatPostingIn):
        actions = await _chat_actions()
        await actions.set_posting_disabled(not payload.enabled, actor="operator",
                                           ttl_s=payload.ttl_s)
        return {"ok": True, "posting_enabled": payload.enabled}

    @app.post("/api/admin/chat/slow_mode", dependencies=[Depends(require_dj)])
    async def admin_chat_slow(payload: ChatSlowIn):
        actions = await _chat_actions()
        if payload.interval_s:
            await actions.set_slow_mode(payload.interval_s, actor="operator")
        else:
            await actions.clear_slow_mode(actor="operator")
        return {"ok": True, "interval_s": payload.interval_s}


def add_admin_routes(app: FastAPI, station: Station) -> None:
    """Admin-only surface: control + DJ console (SPLIT_PLAN §1.2).

    Never registered on the public app. The admin app is a superset, so the
    TUI and existing tests keep working against it.
    """
    _control_routes(app, station)
    _queue_routes(app, station)
    _dj_routes(app, station)
    _payment_routes(app, station)
    _chat_routes(app, station)
