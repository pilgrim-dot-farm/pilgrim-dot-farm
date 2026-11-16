"""Station server (RADIO.md §11).

Two listeners on one Station (SPLIT_PLAN §1): a PUBLIC app with only the
edge-facing routes (program/state/heartbeat/media/requests/visitors/health)
and an ADMIN app with the full API including `/api/admin/*` and station
control. The scheduler + producer run in the background so the station is on
air regardless of whether a listener is tuned in.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import Scope

from pilgrim import dj_contracts as dj
from pilgrim import station_metrics, voting
from pilgrim.album_art import ArtWorker
from pilgrim.api.admin import _admin_health, add_admin_routes
from pilgrim.api.paid import _backfill_paid_summaries
from pilgrim.api.public import _public_health, add_public_routes
from pilgrim.config import (
    PAID_OVERRIDE_ENV,
    RNG,
    ROOT,
    Clock,
    Config,
    ensure_dirs,
    load_api_key,
    load_brave_key,
    load_config,
    load_secret,
    stripe_env,
)
from pilgrim.db import env_summary as db_env_summary
from pilgrim.db import open_store
from pilgrim.hotstate import REDIS_URL_ENV, HotState
from pilgrim.hotstate import connect as cache_connect
from pilgrim.logging_setup import setup_logging
from pilgrim.media_publish import MediaPublisher, S3ObjectStore, art_url, public_url
from pilgrim.payments import (
    PaymentService,
    StripeClient,
)
from pilgrim.pipelines.brave import BraveSearch
from pilgrim.pipelines.copyright import CopyrightScreen
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.moderation import Moderation
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.operator_phrase import PhraseManager
from pilgrim.pipelines.sfx import SfxClient, SfxPipeline
from pilgrim.pipelines.songs import SongPipeline, music_pool_deployments, song_backend_health
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.producer import Producer
from pilgrim.scheduler import Scheduler
from pilgrim.selector import make_selector
from pilgrim.store import Store

log = logging.getLogger("radio.server")


class PublicSurface:
    """What the public routes need from whoever serves them (RADIO §11): the
    station process and the Fargate web tier (docs/pending/scalability.md Step 5,
    `pilgrim/web_app.py`) both inherit this. Client identity through the
    trusted-proxy chain (RADIO §14), the visitor counter, and the Stripe
    setup (docs/shipped/payments.md). Requires `cfg`, `db` and `clock` on the instance;
    the attributes below are what `_add_public_routes` reads."""

    cfg: Config
    db: Store
    clock: Clock
    llm: LLM
    moderator: Moderation
    copyright: CopyrightScreen
    producer: Any
    payments_active: bool
    payment_service: PaymentService | None
    publishable_key: str
    webhook_secret: str
    override_token: str
    prompts: dict
    scheduler: Any  # a Scheduler, or the web tier's cached view (a property there)

    def wake(self, reason: str) -> None:
        raise NotImplementedError

    def program_response(self, after_seq: int | None, client_epoch: str | None = None,
                         client_revision: int | None = None) -> dict:
        raise NotImplementedError

    def public_state(self) -> dict:
        raise NotImplementedError

    def touch_listener(self, player_id: str | None) -> None:
        raise NotImplementedError

    def record_player_telemetry(self, player_id: str | None,
                                payload: dj.HeartbeatIn) -> None:
        raise NotImplementedError

    @staticmethod
    def _canonical_ip(raw: str | None) -> str | None:
        """Canonicalize a client IP string via stdlib ipaddress, folding
        IPv4-mapped IPv6 into IPv4 and taking the first of an XFF list (13)."""
        if not raw:
            return None
        raw = raw.strip()
        if "," in raw:
            raw = raw.split(",")[0].strip()  # leftmost = the client we saw
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return None
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        return ip.compressed

    def _client_ip(self, request: HTTPConnection) -> str | None:
        """Resolve the canonical public client IP through the deployment's
        trusted-proxy chain (SPLIT_PLAN §3.2). A `Request` and a `WebSocket`
        share `.client`/`.headers`, so the chat socket reuses this unchanged.

        A peer is trusted only when its canonical address is inside a
        configured network (`visitors.trusted_proxies`, single IPs and CIDRs).
        From a trusted peer: `CF-Connecting-IP`, else `X-Real-IP`, else the
        first `X-Forwarded-For` entry. From an untrusted peer: the socket peer address; both headers
        are ignored so a spoof cannot reach the visitor counter."""
        peer = request.client.host if (request.client and request.client.host) else None
        if self.cfg.visitors.is_trusted(self._canonical_ip(peer)):
            # Behind Cloudflare -> ALB (docs/pending/scalability.md Step 5) the browser IP
            # is CF-Connecting-IP; X-Forwarded-For may start with whatever the
            # browser sent, since Cloudflare appends rather than replaces.
            header = (request.headers.get("cf-connecting-ip")
                      or request.headers.get("x-real-ip"))
            if not header:
                xff = request.headers.get("x-forwarded-for")
                header = xff.split(",")[0].strip() if xff else None
        else:
            header = peer
        return self._canonical_ip(header)

    def register_visitor(self, request: Request) -> int:
        """Store the raw canonical client IP (deduped) and return the unique
        count, starting from 0. Disabled, an unresolvable identity, or a store
        error all just return the current count — the counter never fails and
        never breaks radio/requests (RADIO §14)."""
        ip = self._client_ip(request) if self.cfg.visitors.enabled else None
        try:
            if ip:
                return self.db.register_visitor(ip)
            log.warning("visitor.register skipped: no canonical client identity")
            return self.db.unique_visitors()
        except Exception:
            log.exception("visitor.register failed")
            return 0

    def request_counts(self) -> dict[str, int]:
        """Paid + total request counts for the HIT COUNTER pane (RADIO §9.1).
        Never fails: a store error reports zeros, exactly like the visitor
        counter, so the pane renders instead of throwing (RADIO §14)."""
        try:
            paid, total = self.db.request_counts()
            return {"paid_requests": paid, "total_requests": total}
        except Exception:
            log.exception("request.counts failed")
            return {"paid_requests": 0, "total_requests": 0}

    def _setup_payments(self, cfg: Config) -> None:
        """Stripe wiring shared by the station and the web tier (docs/shipped/payments.md).
        Needs `self.db` and `self.clock`; never logs a key."""
        # Paid priority requests (docs/shipped/payments.md). Off unless config + keys agree.
        # The keys come from the environment or the repo-root .env fallback
        # (config.stripe_env), so a plain `python -m pilgrim.server` restart
        # does not silently close payments (Step 12 regression, Oct 31).
        stripe_keys = stripe_env()
        self.payments_active = cfg.payments.active(stripe_keys)
        self.payment_service: PaymentService | None = None
        self.stripe_client: StripeClient | None = None
        self.publishable_key = ""
        self.webhook_secret = ""
        if self.payments_active:
            secret_key = stripe_keys["STRIPE_SECRET_KEY"]
            self.publishable_key = stripe_keys["STRIPE_PUBLISHABLE_KEY"]
            self.webhook_secret = stripe_keys["STRIPE_WEBHOOK_SECRET"]
            self.stripe_client = StripeClient(cfg.payments, secret_key, env=stripe_keys)
            self.payment_service = PaymentService(
                cfg.payments, self.db, self.stripe_client, secret_key,
                self.publishable_key, self.webhook_secret, now=self.clock.wall)
            log.info("payment.mode", extra={"mode": self.payment_service.mode})
        cutoff = (datetime.now(UTC)
                  - timedelta(seconds=cfg.payments.abandon_after_s)).isoformat()
        abandoned = self.db.abandon_stale_checkouts(cutoff)
        if abandoned:
            log.info("payment.abandoned_stale", extra={"count": abandoned})
        # Staged checks run as background tasks; a restart orphans them.
        orphaned = self.db.abandon_checking_requests()
        if orphaned:
            log.info("payment.abandoned_checking", extra={"count": orphaned})
        # Operator comp secret (docs/shipped/payments.md Step 12b). Never logged or stored.
        self.override_token = load_secret(PAID_OVERRIDE_ENV)


def _telemetry_from_doc(doc: object) -> dict | None:
    """Normalise the playback telemetry stored with a `player:<id>` key.

    A heartbeat with no telemetry stores `{"at": <ts>}` (the web tier's
    `touch_listener`), which is not a reporting player. Anything with at
    least one telemetry field is (RADIO §9.2).
    """
    if not isinstance(doc, dict):
        return None
    if not any(k in doc for k in ("platform", "ctx_state", "hidden", "sink", "resumed")):
        return None
    return {"platform": str(doc.get("platform") or "other"),
            "ctx_state": str(doc.get("ctx_state") or ""),
            "hidden": bool(doc.get("hidden")),
            "sink": str(doc.get("sink") or ""),
            "resumed": int(doc.get("resumed") or 0)}


class Station(PublicSurface):
    scheduler: Scheduler

    def __init__(self, cfg: Config, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        ensure_dirs(cfg)
        self.media_dir = ROOT / cfg.library.dir
        self.db = open_store(cfg)
        self.db.reset_producing_to_queued()  # crash mid-generation mustn't strand (4.4)
        self.rng = RNG(cfg.station.rng_seed)
        self.llm = LLM(cfg, api_key)
        self.kokoro = KokoroClient(cfg)
        self.prompts = _load_prompts(cfg)
        self.moderator = Moderation(cfg, self.llm, prompts=self.prompts)
        # One Brave client for news + the copyright screen (docs/systems/search.md).
        self.search = BraveSearch(cfg, load_brave_key())
        self.copyright = CopyrightScreen(cfg, self.search)
        self.voice = VoicePipeline(cfg, self.llm, self.kokoro, self.db, self.media_dir,
                                   prompts=self.prompts)
        self.songs = SongPipeline(cfg, self.llm, self.media_dir,
                                  prompts=self.prompts, rng=self.rng, api_key=api_key)
        swept = self.songs.sweep_temp()  # render temp files a crash left behind
        if swept:
            log.info("song.tmp_swept", extra={"count": swept})
        self.news = NewsPipeline(cfg, self.llm, search=self.search,
                                prompts=self.prompts)
        self.sfx_client = SfxClient(cfg)
        self.sfx = SfxPipeline(cfg, self.sfx_client, self.media_dir)
        self.clock = Clock()
        self._listeners: dict[str, float] = {}  # player_id -> last heartbeat (clock.now)
        # Playback telemetry per live player (RADIO §9.2/§11): platform, audio
        # context state, hidden, output sink, interruption recoveries. Pruned
        # with the listener map; transitions are logged (`listener.telemetry`)
        # so background/lock behaviour on phones is visible after the fact.
        self._player_tel: dict[str, dict] = {}
        # docs/shipped/idle.md (RADIO §3): the playhead freezes and production pauses when
        # nobody has been listening for `idle.after_s`. Policy lives here; the
        # mechanics are Scheduler.freeze()/thaw().
        self._idle_since: float | None = None
        self._last_listener_at: float = self.clock.now()
        self.idle_wakes = 0
        self.selector = make_selector(self.rng, cfg)
        self.epoch = uuid.uuid4().hex[:12]  # changes on restart (docs/systems/console.md §5)
        self.scheduler = Scheduler(cfg, self.db, self.selector, self.clock, self.rng,
                                   epoch=self.epoch)
        self.phrases = PhraseManager(cfg, self.db, self.voice, self.media_dir,
                                     scheduler=self.scheduler)
        # Close the phrase lifecycle (RADIO §5.5; docs/systems/console.md §6): when a committed
        # phrase row falls behind the playhead the scheduler marks the job
        # 'aired'; the TTL in PhraseManager._expire handles the never-placed
        # rest. (Hook set post-construction: phrases needs the scheduler,
        # which cannot yet see phrases.)
        self.scheduler.on_phrase_aired = self.phrases.mark_item_aired
        self.producer = Producer(
            cfg, self.db, self.llm, self.kokoro, self.voice, self.songs,
            self.clock, prompts=_load_prompts(cfg), media_dir=self.media_dir,
            api_key=api_key, rng=self.rng, news_pipeline=self.news, sfx=self.sfx,
            idle=self.is_idle)
        # Hot state in Valkey (docs/pending/scalability.md Step 3; RADIO §14). Enabled by
        # REDIS_URL; the client connects lazily so construction never fails.
        # Published from hot_loop() (own task, hard rule 1), never from playout.
        self.hot: HotState | None = None
        self._remote_listeners: set[str] = set()
        cache_url = load_secret(REDIS_URL_ENV)
        if cache_url:
            try:
                self.hot = HotState(cfg, cache_connect(cache_url, cfg), epoch=self.epoch,
                                    now=self.clock.wall)
                log.info("hotstate.enabled", extra={"publish_s": cfg.cache.publish_s})
            except Exception as e:  # noqa: BLE001 - no redis lib / bad URL: degrade
                log.warning("hotstate.disabled", extra={"error": str(e)[:300]})
        self._setup_payments(cfg)  # docs/shipped/payments.md; shared with the web tier
        self._tasks: list = []
        self._http = httpx.AsyncClient(timeout=4.0)
        # Cover art (docs/shipped/album-art.md Step 5): its own task, never inside playout.
        # A song without art airs exactly as today (hard rule 1).
        self.art_worker: ArtWorker | None = None
        # S3 publishing (RADIO §8.4): its own task, never inside playout.
        self.publisher: MediaPublisher | None = None
        if cfg.media.publish:
            try:
                self.publisher = MediaPublisher(cfg, self.db, S3ObjectStore(cfg))
                # RADIO §5.2/§8.4: ready requests wait (bounded) for their
                # audio to reach S3 before the splice, so listeners stream
                # them from CloudFront like every other item.
                self.scheduler.require_published = True
            except Exception as e:  # noqa: BLE001 - no creds/boto3: degrade to /api/media
                log.warning("media.publisher_disabled", extra={"error": str(e)[:300]})
        if cfg.art.enabled:
            try:
                self.art_worker = ArtWorker(cfg, self.db, self.llm, self.prompts)
            except Exception as e:  # noqa: BLE001 - bad config/creds: art is cosmetic
                log.warning("art.worker_disabled", extra={"error": str(e)[:300]})
        # The producer can start a cover from the song brief (early art), in
        # parallel with the song render. Optional: no worker -> worker path only.
        if self.art_worker is not None:
            self.producer.art_worker = self.art_worker

    def media_url(self, item_id: int, inv: dict | None = None) -> str:
        """Browser URL for an item's audio (RADIO §8.4, §11)."""
        if inv is None:
            inv = self.db.get_item(item_id)
        return public_url(self.cfg, inv, item_id)

    def _with_urls(self, overlays: list[dict]) -> list[dict]:
        return [{**ov, "url": self.media_url(int(ov["media_id"]))} for ov in overlays]

    # ------------------------------------------------------------------ health
    async def backend_status(self) -> dict:
        def ok(status: int) -> bool:
            return 200 <= status < 500
        st: dict = {"litellm": False, "kokoro": False, "music": False,
                    "search": False, "sfx": False}
        try:
            r = await self._http.get(
                self.cfg.hosts.litellm.rstrip("/") + "/models", headers=self._auth)
            st["litellm"] = ok(r.status_code)
        except Exception:
            st["litellm"] = False
        try:
            r = await self._http.get(self.cfg.hosts.kokoro.rstrip("/") + "/health")
            st["kokoro"] = ok(r.status_code)
        except Exception:
            st["kokoro"] = False
        st["music_backends"] = await song_backend_health(
            self.cfg, self._http, self.api_key)
        st["music"] = any(st["music_backends"].values())
        # `music_backends` for a proxy backend only says the proxy still lists
        # the model: it is true with every box behind it down. Count the
        # deployments too (RADIO §6.2, MONITORING §2.2) so the dashboard can
        # tell "2 boxes up" from "the model name exists".
        st["music_deployments"] = await music_pool_deployments(
            self.cfg, self._http, self.api_key)
        # effective render slots per backend (discovered for `slots: auto`)
        pool = getattr(self.songs, "pool", None)
        st["music_slots"] = pool.slots() if pool is not None else {}
        # Brave is metered: report the last real search instead of probing
        # (docs/systems/search.md Step 6). True until a call degrades, False without a key.
        st["search"] = self.search.healthy
        st["search_last"] = dict(self.search.last)
        try:
            r = await self._http.get(self.cfg.hosts.sfx.rstrip("/") + "/health")
            st["sfx"] = ok(r.status_code)
        except Exception:
            st["sfx"] = False
        return st

    async def inventory_levels(self) -> dict:
        # same counts the producer refills against (songs/dj/news: unaired and
        # unexpired; commercials/liners: everything usable, they recycle)
        have = self.producer.counts()
        inv = self.cfg.inventory
        return {
            "song": {"have": have["song"], "target": inv.fresh_songs_ready},
            "commercial": {"have": have["commercial"], "target": inv.commercials_min},
            "liner": {"have": have["liner"],
                      "target": inv.liners_per_bucket * len(inv.liner_buckets_s)},
            "dj_talk": {"have": have["dj_talk"], "target": inv.dj_talk_min},
            "field_report": {"have": have["field_report"], "target": inv.field_reports_min},
            "news": {"have": have["news"], "target": 1},
            "sfx": {"have": have["sfx"], "target": self.producer.sfx_stock_target()},
            "promo": {"have": have["promo"], "target": inv.promos_min},
        }

    def rotation(self) -> dict[str, int]:
        """What can air right now (the listener-facing numbers): every
        non-retired song/commercial/liner recycles; DJ talk and news only while
        unaired and unexpired. Unlike `inventory`, songs count aired ones too."""
        have = self.producer.counts()
        return {
            "song": self.db.count_usable_of_type("song"),
            "commercial": have["commercial"],
            "liner": have["liner"],
            "dj_talk": have["dj_talk"],
            "field_report": have["field_report"],
            "news": have["news"],
            "promo": have["promo"],
        }

    def art_health(self) -> dict:
        """Admin-only art health: the worker's cached backend plus DB counts.
        Never probes FLUX (a render blocks /health for ~40 s)."""
        counts = self.db.art_counts()
        backend = self.art_worker.backend if self.art_worker else "disabled"
        return {"backend": backend, **counts}

    @property
    def on_air(self) -> bool:
        return self.scheduler.coverage() > 0

    async def health(self) -> dict:
        backends = await self.backend_status()
        inv = await self.inventory_levels()
        # Heartbeats are handled by the web tier in the split deployment, so
        # the playback telemetry lives in the `player:<id>` keys it writes.
        # Read it here (health cadence only, one GET per live id — MGET across
        # slots is refused by Valkey Serverless) so `listeners` reflects the
        # whole station, not just the players this process happened to see
        # (RADIO §9.2, §11; MONITORING §2.2).
        remote: dict[str, dict] = {}
        if self.hot is not None and self._remote_listeners:
            remote = await asyncio.to_thread(
                self.hot.player_telemetry, sorted(self._remote_listeners))
        # Rendered audio remains playable when production backends fail (§5.4).
        return {
            "on_air": self.on_air,
            "backends": backends,
            "inventory": inv,
            "rotation": self.rotation(),
            "committed_coverage_s": round(self.scheduler.coverage(), 1),
            "committed_count": len(self.scheduler._items),
            "song_realtime": round(self.songs.generation_rate, 2),
            "art": self.art_health(),
            "time": time.time(),
            # docs/shipped/idle.md D10: operator visibility. `on_air` stays true while frozen
            # (coverage > 0), so PLAY is never disabled by idle.
            # docs/pending/scalability.md Step 2: which store engine this process opened (RADIO §14)
            "db": {**db_env_summary(), "engine": self.db.dialect},
            # docs/pending/scalability.md Step 3: the hot-state publisher (RADIO §14)
            "cache": self.hot.summary() if self.hot is not None else {"enabled": False},
            "idle": {"idle": self.is_idle(),
                     "since": self._idle_since,
                     "wakes": self.idle_wakes},
            "listeners": self.listener_telemetry(remote),
        }

    # ------------------------------------------------------------------ program
    def program_response(self, after_seq: int | None,
                         client_epoch: str | None = None,
                         client_revision: int | None = None) -> dict:
        """Committed programme (RADIO §11, §5.3). A stale client cursor
        (epoch/revision mismatch) returns a full snapshot with `reset: true`."""
        scheduler = self.scheduler
        stale = ((client_epoch is not None and client_epoch != scheduler.epoch)
                 or (client_revision is not None
                     and client_revision != scheduler.revision))
        if after_seq is None or stale:
            seq, offset = scheduler.on_air()
            since = seq if seq is not None else scheduler.store.max_seq()
            items = scheduler.store.program_since(since
                                                  if since else 1)
            start_offset = offset
        else:
            items = scheduler.store.program_after(after_seq)
            start_offset = 0.0
        # docs/shipped/ratings.md Step 2 (docs/shipped/voting.md V10): every song row carries its
        # lifetime aggregate so the client can show it the instant the song
        # is audible. One batch query; non-songs and disabled ratings send 0/null.
        song_ids = [it["item_id"] for it in items if it["type"] == "song"]
        aggs = (self.db.song_ratings_for(song_ids)
                if self.cfg.ratings.enabled and song_ids else {})
        out = []
        for it in items:
            inv = self.db.get_item(it["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            agg = aggs.get(it["item_id"]) if it["type"] == "song" else None
            out.append({"seq": it["seq"], "media_id": it["item_id"],
                        "url": self.media_url(it["item_id"], inv),
                        # ALBUM_ART Step 7: "" until the JPEG is in S3; no
                        # origin fallback, art is cosmetic.
                        "art_url": art_url(self.cfg, inv),
                        "type": it["type"], "duration_s": it["duration_s"],
                        "sfx": self._with_urls(scheduler.overlays_for(it["seq"])),
                        "title": title, "artist": artist,
                        "rating_count": agg["count"] if agg else 0,
                        "rating_avg": agg["avg"] if agg else None})
        return {"items": out, "start_offset_s": start_offset,
                "epoch": scheduler.epoch, "revision": scheduler.revision,
                "reset": stale, "server_time": time.time()}

    def public_state(self) -> dict:
        """Cheap station state for DJ-aware clients (RADIO §11, §5.3).
        Reads in-memory schedule state; never probes production backends."""
        st = self.scheduler
        seq = ctype = url = art = None
        offset = 0.0
        if st._items:
            s, off = st.on_air()
            if s is not None:
                row = next((r for r in st._items if r["seq"] == s), None)
                seq, offset = s, off
                ctype = row["type"] if row else None
                if row:
                    inv = self.db.get_item(row["item_id"])
                    url = self.media_url(row["item_id"], inv)
                    art = art_url(self.cfg, inv)
        return {"epoch": st.epoch, "revision": st.revision,
                "server_time": time.time(), "current_seq": seq,
                "current_type": ctype, "current_url": url,
                "current_art_url": art,
                "current_offset_s": round(offset, 3),
                "prepared_cutover": None, "listeners": self.active_listeners(),
                # docs/shipped/idle.md D10: public clients can show the freeze (PLAY is never
                # disabled by it: on_air stays true while coverage > 0).
                "idle": self.is_idle(),
                # docs/shipped/voting.md: the on-air song's rating window + lifetime aggregate
                # (`null` when ratings are disabled). Voter-independent, so the
                # shared max-age=1 cache stays correct.
                "rating": self._public_rating(seq)}

    # ------------------------------------------------------------- idle (docs/shipped/idle.md)
    def is_idle(self) -> bool:
        """True while the playhead is frozen (no listeners for idle.after_s)."""
        return self.scheduler.frozen

    def wake(self, reason: str) -> None:
        """Thaw the frozen station. Synchronous by design (docs/shipped/idle.md D2/rule 1):
        it only touches already-rendered inventory, so the request that woke it
        returns a playable programme in the same response. No-op when awake.

        A thaw failure leaves the station frozen (still playable) rather than
        half-thawed: the next program fetch or heartbeat retries."""
        if not self.scheduler.frozen:
            return
        try:
            frozen_s = (self.clock.now() - self._idle_since
                        if self._idle_since is not None else 0.0)
            dropped = self.scheduler.thaw()
        except Exception as e:  # noqa: BLE001 - a wake must never break radio
            log.exception("station.wake_failed: %s", e)
            return
        self._idle_since = None
        self.idle_wakes += 1
        self._last_listener_at = self.clock.now()
        log.info("station.wake", extra={"reason": reason,
                                        "idle_s": round(frozen_s),
                                        "dropped": dropped})

    def _idle_check(self) -> None:
        """One idle decision (sync so tests can call it directly)."""
        if not self.cfg.idle.enabled or self.scheduler.frozen:
            return
        if self.active_listeners() > 0:
            self._last_listener_at = self.clock.now()
            return
        if self.clock.now() - self._last_listener_at < self.cfg.idle.after_s:
            return
        self.scheduler.freeze()
        self._idle_since = self.clock.now()
        log.info("station.idle", extra={"after_s": self.cfg.idle.after_s,
                                        "coverage_s": round(self.scheduler.coverage(), 1)})

    async def idle_loop(self) -> None:
        """Freeze when nobody is listening (docs/shipped/idle.md D1); wake is immediate, on
        the program/heartbeat/admin request, never on a poll. server.py may use
        asyncio.sleep; the scheduler may not (hard rule 3)."""
        while True:
            try:
                self._idle_check()
            except Exception as e:  # noqa: BLE001 - idle must never break radio
                log.warning("idle.check_failed: %s", e)
            await asyncio.sleep(self.cfg.idle.check_s)

    def hot_tick(self) -> dict | None:
        """One hot-state publish (sync; tests call it directly). Snapshots the
        documents on the event loop thread, then the network round trip is the
        caller's business (hot_loop runs it in a worker thread). Applies the
        results: remote listeners for the idle policy, and a wake flag set by
        the web tier thaws the station (RADIO §3, §14)."""
        if self.hot is None:
            return None
        snapshot = self._hot_snapshot()
        result = self.hot.tick(**snapshot)
        self._apply_hot_result(result)
        return result

    def _hot_snapshot(self) -> dict:
        program = self.program_response(None)
        prev = self._previous_row()
        if prev is not None:
            # The published rows start at the on-air row, so the web tier's
            # rating grace (VOTING V8; FIX_RATINGS Step 1) also needs the
            # row that just ended. Cache document only: the web tier strips it
            # from the HTTP response, so RADIO §11 is unchanged.
            program["prev"] = prev
        return {"program": program,
                "state": self.public_state(),
                "stages": {str(k): v for k, v in self.producer.request_stage.items()},
                "health": {"on_air": self.on_air,
                           "art": {"backend": self.art_worker.backend
                                   if self.art_worker else "disabled"},
                           "time": time.time()}}

    def _apply_hot_result(self, result: dict) -> None:
        self._remote_listeners = set(result.get("players") or ())
        if self._remote_listeners:
            self._last_listener_at = self.clock.now()
        if result.get("wake"):
            self.wake(f"cache:{result['wake']}")

    async def hot_loop(self) -> None:
        """Publish hot state every `cache.publish_s` (docs/pending/scalability.md Step 3).
        The network call runs in a worker thread so a slow cache never stalls
        the event loop; a failure is logged by HotState and retried next tick."""
        assert self.hot is not None
        while True:
            try:
                snapshot = self._hot_snapshot()
                result = await asyncio.to_thread(self.hot.tick, **snapshot)
                self._apply_hot_result(result)
                if result.get("wake"):
                    continue   # publish the thawed programme right away, not a tick later
            except Exception as e:  # noqa: BLE001 - the cache must never break radio
                log.warning("hotstate.tick_failed: %s", e)
            await asyncio.sleep(self.cfg.cache.publish_s)

    def _previous_row(self) -> dict | None:
        """The committed row just before the on-air one, in the programme-row
        shape `CachedScheduler` reads (`seq`, `media_id`, `type`,
        `duration_s`), or None at the head of the window."""
        st = self.scheduler
        on_seq, _offset = st.on_air()
        if on_seq is None:
            return None
        items = st._items
        idx = next((i for i, r in enumerate(items) if r["seq"] == on_seq), None)
        if not idx:          # None (not in the window) or 0 (nothing before it)
            return None
        r = items[idx - 1]
        return {"seq": int(r["seq"]), "media_id": int(r["item_id"]),
                "type": r["type"], "duration_s": float(r["duration_s"])}

    def _public_rating(self, seq: int | None) -> dict | None:
        """The on-air item's rating window + lifetime aggregate (docs/shipped/voting.md V10).
        Zeroes when the on-air row is not a song; null when ratings are off."""
        if not self.cfg.ratings.enabled:
            return None
        st = self.scheduler
        open_, item_id = voting.rating_window(
            st, st.epoch, int(seq) if seq is not None else -1,
            self.cfg.ratings.grace_s)
        if open_ and item_id is not None:
            agg = self.db.song_rating(item_id)
            return {"open": True, "count": agg["count"], "avg": agg["avg"]}
        return {"open": False, "count": 0, "avg": None}

    # ------------------------------------------------------------- listeners
    def _prune_listeners(self) -> None:
        cutoff = self.clock.now() - self.cfg.visitors.listener_timeout_s
        for pid in [p for p, t in self._listeners.items() if t < cutoff]:
            del self._listeners[pid]
            self._player_tel.pop(pid, None)

    def record_player_telemetry(self, player_id: str | None,
                                payload: dj.HeartbeatIn) -> None:
        """Keep the latest playback telemetry per live player and log a
        `listener.telemetry` line whenever it changes (RADIO §9.2/§11). Only
        players already in the listener map are kept (bounded by
        `visitors.max_listeners`); a heartbeat without telemetry is ignored."""
        if not player_id or player_id not in self._listeners:
            return
        if (payload.platform is None and payload.ctx_state is None
                and payload.hidden is None and payload.sink is None
                and payload.resumed is None):
            return
        tel = {"platform": payload.platform or "other",
               "ctx_state": payload.ctx_state or "",
               "hidden": bool(payload.hidden),
               "sink": payload.sink or "",
               "resumed": int(payload.resumed or 0)}
        prev = self._player_tel.get(player_id)
        self._player_tel[player_id] = tel
        if self.hot is not None:
            self.hot.queue_player(player_id, tel)
        if prev != tel:
            log.info("listener.telemetry", extra={
                "player_id": player_id[:16], **tel,
                "underrun": bool(payload.underrun)})

    def listener_telemetry(self, remote: dict[str, dict] | None = None) -> dict:
        """Admin health summary of live players' playback telemetry.

        `remote` is what `HotState.player_telemetry()` read from the
        `player:<id>` keys the web tier writes: in the split deployment every
        browser heartbeat lands there, never on this process, so the
        in-memory map alone would always report zero (RADIO §9.2, §11).
        `count` is the same union `active_listeners()` returns."""
        self._prune_listeners()
        remote = remote or {}
        ids = set(self._listeners) | set(self._remote_listeners) | set(remote)
        tels: list[dict] = []
        for pid in sorted(ids):
            tel = self._player_tel.get(pid) or _telemetry_from_doc(remote.get(pid))
            if tel is not None:
                tels.append(tel)
        by_platform: dict[str, int] = {}
        for t in tels:
            by_platform[t["platform"]] = by_platform.get(t["platform"], 0) + 1
        return {
            "count": len(ids),
            "reporting": len(tels),
            "by_platform": by_platform,
            "hidden": sum(1 for t in tels if t["hidden"]),
            "not_running": sum(1 for t in tels
                               if t["ctx_state"] and t["ctx_state"] != "running"),
            "sink_element": sum(1 for t in tels if t["sink"] == "element"),
            "resumed_total": sum(t["resumed"] for t in tels),
        }

    def touch_listener(self, player_id: str | None) -> None:
        """Mark a player as listening (RADIO §9.1, §14). In-memory only: a
        player counts while its heartbeats arrive within
        `visitors.listener_timeout_s`. New IDs past `visitors.max_listeners`
        are ignored so client-supplied IDs can't grow the map unbounded."""
        if not player_id or len(player_id) > 64:
            return
        self._prune_listeners()
        if (player_id in self._listeners
                or len(self._listeners) < self.cfg.visitors.max_listeners):
            self._listeners[player_id] = self.clock.now()
            self._last_listener_at = self.clock.now()
            if self.hot is not None and player_id not in self.hot._pending_players:
                self.hot.queue_player(player_id, {})
            # docs/shipped/idle.md D2: a heartbeat is a listener; wake before the handler
            # reads anything so the response reflects the thawed programme.
            self.wake("heartbeat")

    def listener_count(self) -> int:
        """Live players, read-only: the same union `active_listeners()`
        returns, without pruning, so a metrics tick never mutates station
        state (docs/shipped/monitoring.md §6 rule 6). Stale entries leave on the next
        heartbeat, which is where they are pruned today."""
        return len(set(self._listeners) | self._remote_listeners)

    def active_listeners(self) -> int:
        """Live players: the in-memory map plus the heartbeat keys another tier
        wrote to the cache (RADIO §14 "Hot state"), as a union so a player seen
        both ways counts once."""
        self._prune_listeners()
        return len(set(self._listeners) | self._remote_listeners)

    # ---------------------------------------------------------------- DJ (§11)
    def dj_state(self) -> dict:
        """Read-only console state — computed in-process, no backend probes
        (docs/systems/console.md §4)."""
        st = self.scheduler
        on_air = self.on_air
        current = None
        if st._items:
            seq, off = st.on_air()
            row = next((r for r in st._items if r["seq"] == seq), None)
            if seq is not None and row is not None:
                inv = self.db.get_item(row["item_id"])
                title = (inv.get("title") if inv else None) or ""
                artist = (inv.get("artist") if inv else None) or ""
                current = {"seq": row["seq"], "media_id": row["item_id"],
                           "type": row["type"], "title": title,
                           "artist": artist, "position_s": round(off, 3),
                           "remaining_s": round(max(0.0, row["duration_s"] - off), 3)}
        floor_seq = current["seq"] if current else -1
        upcoming = []
        for r in st._items:
            if r["seq"] <= floor_seq:
                continue
            inv = self.db.get_item(r["item_id"])
            title = (inv.get("title") if inv else None) or ""
            artist = (inv.get("artist") if inv else None) or ""
            upcoming.append({"seq": r["seq"], "media_id": r["item_id"],
                             "type": r["type"], "title": title,
                             "artist": artist, "duration_s": r["duration_s"]})
            if len(upcoming) >= 12:
                break
        return {"epoch": st.epoch, "revision": st.revision,
                "server_time": time.time(), "on_air": on_air,
                "current": current, "upcoming": upcoming,
                "pending_commands": [],
                "catalogue_revision": self.db.catalogue_revision(),
                "player_status": "unknown"}

    @staticmethod
    def _encode_cursor(c: tuple[str, str, int] | None) -> str | None:
        if c is None:
            return None
        return base64.urlsafe_b64encode(
            json.dumps(list(c)).encode()).decode()

    @staticmethod
    def _decode_cursor(s: str | None) -> tuple[str, str, int] | None:
        if not s:
            return None
        try:
            raw = json.loads(base64.urlsafe_b64decode(s.encode()))
            return str(raw[0]), str(raw[1]), int(raw[2])
        except Exception:
            raise HTTPException(
                422, detail={"code": "invalid_cursor",
                             "detail": "bad /api/admin/dj/songs cursor"}) from None

    def dj_songs(self, q: str | None, sort: str, direction: str,
                 cursor: str | None, limit: int) -> dict:
        """Read-only song catalogue with advisory eligibility (docs/systems/console.md §4)."""
        cur = self._decode_cursor(cursor)
        rows, nxt, cat_rev = self.db.song_catalogue(q, sort, direction, cur, limit)
        floor = float(min(self.cfg.playout.song_min_spacing_s))
        earliest_wall = self.clock.wall() + self.scheduler.coverage()
        items = []
        for r in rows:
            last = r["last_aired_at"]
            eligible = (bool(r["fresh"]) or last is None
                        or (earliest_wall - last) >= floor)
            items.append({"id": r["id"], "title": r["title"] or "",
                          "artist": r["artist"] or "",
                          "duration_s": r["duration_s"], "eligible": eligible,
                          "reason": None if eligible
                          else "repeats too soon (min spacing)"})
        return {"items": items, "next_cursor": self._encode_cursor(nxt),
                "catalogue_revision": cat_rev}

    # -------------------------------------------------------------- visitors
    async def startup(self) -> None:
        # warm the program immediately with whatever inventory exists
        self.scheduler.commit_lookahead()
        self._tasks.append(asyncio.create_task(self.scheduler.run()))
        self._tasks.append(asyncio.create_task(self.phrases.run()))
        self._tasks.append(asyncio.create_task(self.producer.run()))
        self._tasks.append(asyncio.create_task(self.producer.news_loop()))
        self._tasks.append(asyncio.create_task(self.producer.song_loop()))
        self._tasks.append(asyncio.create_task(self.producer.sfx_loop()))
        self._tasks.append(asyncio.create_task(self.idle_loop()))
        if self.hot is not None:
            self._tasks.append(asyncio.create_task(self.hot_loop()))
        self._tasks.append(asyncio.create_task(_backfill_paid_summaries(self)))
        if self.publisher is not None:
            self._tasks.append(asyncio.create_task(self.publisher.run()))
        if self.art_worker is not None:
            self._tasks.append(asyncio.create_task(self.art_worker.run()))
        inv = await self.inventory_levels()
        log.info("station.startup", extra={
            "lookahead_s": self.cfg.playout.committed_lookahead_s, "inventory": inv})


def _load_prompts(cfg: Config) -> dict:
    pdir = ROOT / cfg.library.prompts_dir
    out = {}
    for f in ("voice.md", "song_brief.md", "station_song.md", "news.md", "moderation.md",
              "field.md", "sfx_cues.md", "sfx_joke.md", "paid_request.md",
              "paid_summary.md", "promo.md", "album_art_scene.md",
              "album_art_flux.md", "chat_moderation.md", "field_pen.md"):
        p = pdir / f
        if p.exists():
            out[f.replace(".md", "")] = p.read_text()
    return out


def build_station(cfg: Config) -> Station:
    """Create the one Station for this process (store, pipelines, scheduler)."""
    setup_logging(cfg)
    api_key = load_api_key()
    if not api_key:
        logging.getLogger("radio.server").warning(
            "LITELLM_TOKEN not set (env or .env); LLM and MCP calls will fail")
    if not load_brave_key():
        logging.getLogger("radio.server").warning(
            "BRAVE_SEARCH_KEY not set (env or .env); news runs without search "
            "and the copyright screen reports unchecked")
    return Station(cfg, api_key)


class _RevalidatingStaticFiles(StaticFiles):
    """Static client files are served with `Cache-Control: no-cache`, so a
    browser (or Cloudflare) revalidates them on each load instead of keeping a
    heuristically-fresh copy: a web edit goes live on the next page load with
    no server restart and no cache-busting build step (RADIO §11). Edges set
    their own `max-age=60` for the copy they serve; media keeps its own
    immutable header."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def _mount_web(app: FastAPI, cfg: Config) -> None:
    """Mount the static client at "/" (RADIO §9, §11). Mounted last so API
    routes take precedence; html=True serves index.html at "/"."""
    web_dir = ROOT / cfg.library.web_dir
    app.mount("/", _RevalidatingStaticFiles(directory=str(web_dir), html=True),
              name="web")



def _new_app(title: str) -> FastAPI:
    app = FastAPI(title=title, version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def _no_store_posts(request: Request, call_next):
        """SPLIT_PLAN §2: every POST is no-store, including error responses.

        GET cache policy is set by the individual handlers; static files are
        left to the edge.
        """
        response = await call_next(request)
        if request.method == "POST":
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _add_api_404(app: FastAPI) -> None:
    """Any `/api/*` path the public app does not register is 404, never 405.

    The static mount at "/" would otherwise answer unmatched POSTs with 405
    (SPLIT_PLAN §1.5 expects 404 for control routes on the public listener).
    Registered before the static mount; real routes are matched first.
    """

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT",
                                               "PATCH", "DELETE", "HEAD",
                                               "OPTIONS"],
                   include_in_schema=False)
    async def _unknown_api(rest: str):
        raise HTTPException(404, "not found")


def create_public_app(cfg: Config, station: Station) -> FastAPI:
    """The public listener: only the edge-facing routes (§1.2)."""
    app = _new_app("Pilgrim Dot Farm Radio")
    app.state.station = station
    add_public_routes(app, station, _public_health)
    _add_api_404(app)
    if cfg.station.serve_web:
        _mount_web(app, cfg)
    return app


def create_admin_app(cfg: Config, station: Station) -> FastAPI:
    """The admin listener: full API, no static web mount (§1.2)."""
    app = _new_app("Pilgrim Dot Farm Radio (admin)")
    app.state.station = station
    add_public_routes(app, station, _admin_health)
    add_admin_routes(app, station)
    return app


def create_app(cfg: Config | None = None) -> FastAPI:
    """Legacy single-app factory for tests (kept name + signature).

    Returns the admin app with the static web mount and the deferred startup
    hook, exactly as before (SPLIT_PLAN §1.2). Production uses `main()`.
    """
    cfg = cfg or load_config()
    station = build_station(cfg)
    app = create_admin_app(cfg, station)
    if cfg.station.serve_web:
        _mount_web(app, cfg)

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(_post_start(station))

    return app


async def _post_start(station: Station) -> None:
    await asyncio.sleep(0.5)
    await station.startup()


def listener_configs(cfg: Config, public: FastAPI, admin: FastAPI) -> list:
    """uvicorn configs for the public and admin listeners (SPLIT_PLAN §1.2).

    `proxy_headers=False`: uvicorn would otherwise rewrite `request.client`
    from `X-Forwarded-For` for loopback peers before our code runs, a second
    trust decision beside `visitors.trusted_proxies` (SPLIT_PLAN §3.2).
    `log_config=None`: uvicorn's lines propagate to the root JSON logger
    (AGENTS §5).
    """
    import uvicorn

    return [
        uvicorn.Config(public, host=cfg.station.host, port=cfg.station.port,
                       log_config=None, proxy_headers=False),
        uvicorn.Config(admin, host=cfg.station.admin_host,
                       port=cfg.station.admin_port, log_config=None,
                       proxy_headers=False),
    ]


def main() -> None:
    import signal

    import uvicorn

    cfg = load_config()
    station = build_station(cfg)
    public = create_public_app(cfg, station)
    admin = create_admin_app(cfg, station)

    class _Server(uvicorn.Server):
        """uvicorn.Server without its own signal capture.

        Two servers share one event loop, so each would install SIGINT/SIGTERM
        and the second install would replace the first — leaving one listener
        running after Ctrl-C/SIGTERM. `main()` installs a single handler that
        stops both (SPLIT_PLAN §1.2, deviation noted in the report).
        """

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    servers = [_Server(c) for c in listener_configs(cfg, public, admin)]

    def _shutdown(signum: int, frame: object) -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    async def _serve() -> None:
        await station.startup()  # exactly once per process
        station_metrics.start(cfg, station)  # docs/shipped/monitoring.md: own socket + task
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
