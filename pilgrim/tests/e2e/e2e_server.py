"""E2E server for the Playwright client gap test (RADIO.md §15, M9).

Boots the REAL scheduler (SimClock-free, real Clock) over a temp Store seeded
with SHORT rendered MP3 items (the delivery format, RADIO §8.2) generated on
the fly (no real backends — AGENTS §2). Exposes just the endpoints the client
needs. Run by the Playwright
webServer so the browser can exercise PLAY -> heartbeats -> gapless joins.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pilgrim import voting
from pilgrim.audio.normalize import encode_mp3
from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import NullAudit
from pilgrim.chat.giles import Giles
from pilgrim.chat.hub import ChatHub
from pilgrim.chat.redis_store import ChatStore
from pilgrim.chat.review import Reviewer
from pilgrim.chat.routes import register_chat_routes
from pilgrim.config import RNG, Clock, Config, ensure_dirs, load_config
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.server import _load_prompts, cat_manifest
from pilgrim.store import Store
from pilgrim.tests.fakes import FakeLLM
from pilgrim.tests.fakes.fake_flux import encode_png
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis

_CFG = load_config()


def _make_audio(dst: Path, seconds: float) -> None:
    """A tone encoded exactly like production delivery (48 kHz stereo MP3 with
    a gapless header), so the gap test exercises the browser's MP3 trimming."""
    sr = 24000
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    x = (0.25 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    src = dst.with_suffix(".src.flac")
    sf.write(src, x, sr, format="FLAC")
    encode_mp3(src, dst, _CFG, _CFG.audio.mp3_mono_source_kbps)
    src.unlink()


def _seed(cfg: Config, store: Store, tmp: Path) -> None:
    lib = tmp / "library"
    lib.mkdir(parents=True, exist_ok=True)
    genres = list(cfg.songs.genres.keys())
    for i in range(30):  # songs
        f = lib / f"song_{i}.mp3"
        _make_audio(f, 1.5 + (i % 3) * 0.5)
        sid = store.add_item(type_="song", media_path=str(f), duration_s=1.5 + (i % 3) * 0.5,
                             genre=genres[i % len(genres)], evergreen=True, fresh=True,
                             title=f"Song {i}", artist="The Fakes")
        # cover art (docs/shipped/album-art.md Step 8): every song has one, served locally
        art = lib / f"art_{i}.png"
        art.write_bytes(encode_png(48, 48, color=(20 + i, 140, 90)))
        store.set_art(sid, str(art), {"scene": "e2e", "seed": i})
    # one short SFX stinger that rides on talk clips (docs/systems/sfx.md §7.4): exercises
    # the client's overlay path without touching the host joins
    sfx = lib / "sfx_0.mp3"
    _make_audio(sfx, 0.5)
    sfx_id = store.add_item(type_="sfx", media_path=str(sfx), duration_s=0.5,
                            meta={"cue": "cow"})
    cue = {"item_id": sfx_id, "cue": "cow", "kind": "stinger", "offset_s": 0.6,
           "duration_s": 0.5}
    for t, n in (("liner", 20), ("commercial", 20), ("dj_talk", 15)):
        for j in range(n):
            f = lib / f"{t}_{j}.mp3"
            _make_audio(f, 2.0 + (j % 3) * 0.5)
            store.add_item(type_=t, media_path=str(f),
                           duration_s=2.0 + (j % 3) * 0.5, evergreen=True, fresh=True,
                           meta={"sfx": [cue]} if j % 2 == 0 else None)


def _load_cfg() -> tuple[Config, Path]:
    base = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="radio_e2e_"))
    base.library.dir = str(tmp / "library")
    base.library.db = str(tmp / "station.db")
    # 2 s clips: scale the song spacing floors to match, so 30 songs behave like
    # a stocked library, not a song drought (which only bridges ~60 s, §5.2)
    base.playout.song_min_spacing_s = [30, 20, 10]
    ensure_dirs(base)
    return base, tmp


def _seed_requests(cfg: Config, store: Store) -> None:
    """docs/plans/voting.md Step 7: a real queue board — 12 free requests plus one paid
    priority row (paid rows are not votable, V5) — and 5 aired requests so the
    AIRED REQUESTS table keeps its prep-time coverage (cosmetic.spec.ts)."""
    # Aired first (low ids) so a later airing of a queue row still lands in
    # the board's "5 most recent"; k=1 stays unmeasured (an em dash).
    song_ids = [i["id"] for i in store.list_items("song")][:5]
    base = 1_800_000_000.0
    for k in range(1, 6):
        r = store.add_request(f"played {k}", cap=None)
        store.mark_request_ready(r["id"], song_ids[k - 1])
        store.mark_request_aired(r["id"])
        if k >= 2:
            start = datetime.fromtimestamp(base + k * 60, tz=UTC).isoformat()
            end = datetime.fromtimestamp(base + k * 60 + k * 900, tz=UTC).isoformat()
            with store._lock:
                store._conn.execute(
                    "UPDATE requests SET prep_started_at=?, aired_at=? WHERE id=?",
                    (start, end, r["id"]))
                store._conn.commit()
    for i in range(1, 13):
        store.add_request(f"e2e free request {i}", cap=None)
    paid = store.add_paid_request("e2e paid request")
    store.attach_checkout(paid["id"], f"cs_e2e_{paid['id']}")
    store.mark_request_paid(
        paid["id"], session_id=f"cs_e2e_{paid['id']}",
        payment_intent_id="pi_e2e", amount_cents=350, currency="usd",
        livemode=False)


def build_app() -> FastAPI:
    cfg, tmp = _load_cfg()
    store = Store(tmp / "station.db")
    _seed(cfg, store, tmp)
    _seed_requests(cfg, store)
    clock = Clock()
    rng = RNG(1)
    sched = Scheduler(cfg, store, RandomSelector(rng, cfg), clock, rng)
    sched.commit_lookahead()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(sched.run())
        await chat_hub.start()
        try:
            yield
        finally:
            await chat_hub.stop()
            task.cancel()

    app = FastAPI(title="pilgrim e2e", lifespan=_lifespan)
    app.state.store = store
    app.state.sched = sched

    # ---- THE SHEEP PEN (docs/plans/sheep-pen-brief.md §12 e2e): the real routes over the async
    # fake Valkey, with fakes for Layer B and Giles. ----
    chat_clock = Clock()
    chat_client = FakeAsyncRedis(lambda: time.time())
    chat_cache = ChatStore(cfg, chat_client)
    chat_hub = ChatHub(cfg, chat_cache, store, chat_clock)
    chat_audit = NullAudit()
    chat_actions = ChatActions(cfg, store, chat_cache, chat_audit, chat_clock)
    chat_llm = FakeLLM(responses={
        "Review the messages": {
            "decisions": [], "summary": "e2e"},
        "Write one dispatch": {
            "text": "The sheep pen has been debating tractors. "
                    "Outstanding in my field, apparently. "
                    "This is Giles signing off with a reminder that I'm "
                    "outstanding in my field."},
    })
    chat_prompts = _load_prompts(cfg)
    chat_reviewer = Reviewer(cfg, store, chat_cache, chat_actions, chat_llm,  # type: ignore[arg-type]
                             chat_prompts, clock=chat_clock, audit=chat_audit)
    chat_giles = Giles(cfg, store, chat_cache, chat_llm, chat_prompts,  # type: ignore[arg-type]
                       clock=chat_clock, audit=chat_audit)

    class _ChatStation:
        def __init__(self) -> None:
            self.cfg = cfg

        @staticmethod
        def _client_ip(conn):
            return conn.client.host if (conn.client and conn.client.host) else None

    register_chat_routes(app, _ChatStation(), store, chat_cache, chat_hub,
                         chat_audit, chat_clock)

    @app.post("/api/test/chat/giles")
    async def test_giles():
        """Test-only: clear the cadence gates and file one Giles dispatch."""
        await chat_cache.set("giles:last", "0")
        await chat_cache.set_json("giles:state", {"last_active_ts": 0,
                                                   "last_idle_ts": 0})
        out = await chat_giles.maybe_dispatch()
        return {"ok": out is not None, "dispatch": out}

    @app.post("/api/test/chat/delete/{message_id}")
    async def test_delete(message_id: str):
        return {"ok": await chat_actions.delete_message(message_id,
                                                        actor="e2e",
                                                        reason="e2e delete")}

    @app.post("/api/test/chat/ban")
    async def test_ban(user_id: str):
        until = await chat_actions.ban_user(user_id, "e2e ban", actor="e2e",
                                            until=time.time() + 600)
        return {"ok": True, "until": until}

    @app.post("/api/test/chat/unban")
    async def test_unban(user_id: str):
        return {"ok": True,
                "cleared": await chat_actions.unban("user", user_id, actor="e2e")}

    @app.post("/api/test/chat/posting")
    async def test_posting(enabled: bool = True):
        await chat_actions.set_posting_disabled(not enabled, actor="e2e")
        return {"ok": True}

    @app.post("/api/test/chat/slow")
    async def test_slow(interval_s: float = 10.0):
        await chat_actions.set_slow_mode(interval_s, actor="e2e")
        return {"ok": True}

    @app.post("/api/test/chat/review")
    async def test_review():
        return await chat_reviewer.run_once()

    @app.post("/api/station/start")
    async def start():
        return {"ok": True}

    @app.post("/api/station/stop")
    async def stop():
        return {"ok": True}

    @app.get("/api/station/program")
    async def program(after_seq: int | None = None, epoch: str | None = None,
                      revision: int | None = None):
        sched.commit_lookahead()
        stale = ((epoch is not None and epoch != sched.epoch)
                 or (revision is not None and revision != sched.revision))
        if after_seq is None or stale:
            seq, offset = sched.on_air()
            since = seq if seq is not None else store.max_seq()
            items = store.program_since(since if since else 1)
            start_offset = offset
        else:
            items = store.program_after(after_seq)
            start_offset = 0.0
        aggs = store.song_ratings_for(
            [it["item_id"] for it in items if it["type"] == "song"])
        out = []
        for it in items:
            agg = aggs.get(it["item_id"]) if it["type"] == "song" else None
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": sched.overlays_for(it["seq"]),
                        "title": (store.get_item(it["item_id"]) or {}).get("title") or "",
                        "artist": (store.get_item(it["item_id"]) or {}).get("artist") or "",
                        "art_url": _art_url(store, it["item_id"]),
                        "rating_count": agg["count"] if agg else 0,
                        "rating_avg": agg["avg"] if agg else None})
        return JSONResponse({"items": out, "start_offset_s": start_offset,
                "epoch": sched.epoch, "revision": sched.revision,
                "reset": stale, "server_time": time.time()},
                headers={"Cache-Control": "public, max-age=1"})

    @app.get("/api/station/state")
    async def state():
        seq, offset = sched.on_air()
        row = next((r for r in sched._items if r["seq"] == seq), None)
        return JSONResponse({"epoch": sched.epoch, "revision": sched.revision,
                "server_time": time.time(), "current_seq": seq,
                "current_type": row["type"] if row else None,
                "current_offset_s": offset, "prepared_cutover": None,
                "listeners": 0, "idle": sched.frozen,
                "rating": _rating_state()},
                headers={"Cache-Control": "public, max-age=1"})

    def _rating_state():
        """e2e mirror of Station._public_rating (docs/plans/voting.md V10)."""
        if not cfg.ratings.enabled:
            return None
        seq, _ = sched.on_air()
        open_, item_id = voting.rating_window(
            sched, sched.epoch, int(seq) if seq is not None else -1,
            cfg.ratings.grace_s)
        if open_ and item_id is not None:
            agg = store.song_rating(item_id)
            return {"open": True, "count": agg["count"], "avg": agg["avg"]}
        return {"open": False, "count": 0, "avg": None}

    @app.post("/api/station/heartbeat")
    async def heartbeat(payload: dict):
        store.record_airplay(
            item_id=int(payload.get("seq", 0) or 0),
            seq=int(payload.get("seq", 0) or 0),
            item_type=payload.get("type"),
            started_at=payload.get("started_at"),
            position=payload.get("position"),
            underrun=1 if payload.get("underrun") else 0)
        return JSONResponse({"ok": True},
                            headers={"Cache-Control": "no-store"})

    def _art_url(store: Store, item_id: int) -> str:
        """e2e mirror of art_url (ALBUM_ART Step 7): the local art route when
        the item has a rendered cover, else ""."""
        it = store.get_item(item_id) or {}
        return f"/api/art/{item_id}" if it.get("art_path") else ""

    @app.get("/api/art/{item_id}")
    async def art(item_id: int):
        it = store.get_item(item_id)
        if not it or not it.get("art_path"):
            return JSONResponse({"detail": "no art"}, status_code=404)
        return FileResponse(str(it["art_path"]), media_type="image/png",
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})

    @app.get("/api/media/{item_id}")
    async def media(item_id: int):
        it = store.get_item(item_id)
        if not it:
            return JSONResponse({"detail": "no such item"}, status_code=404,
                                headers={"Cache-Control": "public, max-age=30"})
        p = Path(it["media_path"])
        return FileResponse(str(p), media_type="audio/mpeg",
                            headers={"Cache-Control":
                                     "public, max-age=31536000, immutable"})

    @app.get("/api/health")
    async def health():
        return JSONResponse({"on_air": True,
                "committed_coverage_s": sched.coverage(),
                "inventory": {}, "time": time.time()},
                headers={"Cache-Control": "public, max-age=2"})

    @app.post("/api/visitors")
    async def visitors(request: Request):
        """e2e mirror of RADIO §14: store the server-derived peer IP and
        return the unique count (current count when no identity), plus the
        HIT COUNTER request counts (RADIO §9.1)."""
        peer = request.client.host if (request.client and request.client.host) else None
        n = store.register_visitor(peer) if peer else store.unique_visitors()
        paid, total = store.request_counts()
        return JSONResponse({"unique_visitors": n, "paid_requests": paid,
                             "total_requests": total},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/requests")
    async def list_requests():
        board = store.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)
        return JSONResponse({**board, "cap": cfg.requests.queue_cap,
                             "paid_enabled": True,
                             "voting_enabled": cfg.voting.enabled},
                            headers={"Cache-Control": "public, max-age=2"})

    @app.post("/api/requests")
    async def submit_request(payload: dict):
        store.add_request(str(payload.get("text") or "e2e request"), cap=None)
        board = store.request_board(
            cfg.requests.queue_cap, cfg.requests.max_length,
            voting_enabled=cfg.voting.enabled,
            bury_threshold=cfg.voting.bury_threshold)
        return JSONResponse({"ok": True, "rejected": False, **board,
                "cap": cfg.requests.queue_cap, "paid_enabled": True,
                "voting_enabled": cfg.voting.enabled},
                headers={"Cache-Control": "no-store"})

    @app.post("/api/paid-requests")
    async def submit_paid_request(payload: dict):
        """docs/shipped/prompt-upgrade.md Step 5: the three-box shape the real route takes,
        validated the same way, but nothing is stored and payment is a fake."""
        lyrics = str(payload.get("lyrics") or "").strip()
        music = str(payload.get("music") or "").strip()
        art = str(payload.get("art") or "").strip()
        parts = cfg.payments.parts
        if not lyrics and not music:
            return JSONResponse(status_code=422, content={
                "detail": "tell us what the song is about or how it should sound"})
        for name, value, cap in (("Lyrics", lyrics, parts.lyrics_max),
                                 ("Musical direction", music, parts.music_max),
                                 ("Cover art", art, parts.art_max)):
            if len(value) > cap:
                return JSONResponse(status_code=422, content={
                    "detail": f"{name} is too long ({cap} characters max)"})
        return JSONResponse(content={
            "ok": False, "rejected": True,
            "reason": "e2e: payments are a fake here"},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/requests/{request_id}/vote")
    async def vote(request_id: int, payload: voting.VoteIn):
        status, body = voting.cast_vote(
            store, cfg, request_id, payload.voter_id, payload.value, None)
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/station/rating")
    async def rate(payload: voting.RatingIn):
        status, body = voting.cast_rating(store, cfg, sched, payload, None)
        return JSONResponse(status_code=status, content=body,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/test/air/{request_id}")
    async def test_air(request_id: int):
        """Test-only: mark a request aired so the spec can watch the row lose
        its vote buttons on the next board poll (docs/plans/voting.md V4)."""
        store.mark_request_aired(request_id)
        return {"ok": True}

    @app.post("/api/test/splice")
    async def splice():
        """Test-only: submit a rendered request and let the scheduler splice it
        into the next song slot (revision-aware client reset, docs/systems/console.md §5)."""
        lib = tmp / "library"
        song_f = lib / "req_song.mp3"
        intro_f = lib / "req_intro.mp3"
        # 2.2 s: no stock clip has this length, so the request song's byte size
        # is unique (the dead-air spec hangs the decode of exactly these bytes)
        _make_audio(song_f, 2.2)
        _make_audio(intro_f, 0.8)
        req = store.add_request("e2e request", cap=10)
        song = store.add_item(type_="song", media_path=str(song_f), duration_s=2.2,
                              fresh=True, title="Request Song", artist="Listener")
        intro = store.add_item(type_="intro", media_path=str(intro_f), duration_s=0.8,
                               evergreen=False, fresh=True,
                               meta={"song_item_id": song, "request_id": req["id"]})
        store.mark_request_ready(req["id"], song, intro)
        sched.commit_lookahead()
        song_seq = next((r["seq"] for r in sched._items if r["item_id"] == song), None)
        return {"ok": True, "revision": sched.revision,
                "song_id": song, "song_seq": song_seq}

    @app.get("/api/station/cats")
    async def cats():
        """Same pool as production (RADIO §11): the live-cover rotation the
        cover-art spec samples."""
        return JSONResponse({"cats": cat_manifest(cfg)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/test/report")
    async def report():
        """Test-only: airplay stats for the gap assertion (§15)."""
        rows = store.recent_airplay(500)
        # Heartbeats include periodic updates for the same clip; report starts
        # in chronological order, collapsing only consecutive duplicates.
        seqs: list[int] = []
        for row in reversed(rows):
            if row["item_type"] and (not seqs or row["seq"] != seqs[-1]):
                seqs.append(row["seq"])
        underrun = sum(1 for r in rows if r["underrun"])
        # SPLIT_PLAN §7.3 check 8: the origin-side canonical visitor IPs, so
        # the smoke test can prove a spoofed X-Real-IP was discarded.
        ips = [r[0] for r in store._conn.execute(
            "SELECT ip FROM visitor_ips ORDER BY ip").fetchall()]
        return {"seqs": seqs, "underrun": underrun,
                "coverage_s": sched.coverage(),
                "committed_lookahead_s": sched.cfg.playout.committed_lookahead_s,
                "refill_interval_s": sched.REFILL_INTERVAL_S,
                "visitor_ips": ips}

    web_dir = Path(__file__).resolve().parents[2] / "web"
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


app = build_app()
