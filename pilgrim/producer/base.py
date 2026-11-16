"""Producer state and the helpers every content type shares (RADIO §6)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pilgrim.config import RNG, Clock, Config
from pilgrim.pipelines.clocktime import (
    current_local_time,
)
from pilgrim.pipelines.llm import LLM
from pilgrim.pipelines.news import NewsPipeline
from pilgrim.pipelines.sfx import SFX_HOST_TYPES, SfxPipeline
from pilgrim.pipelines.songs import SongPipeline
from pilgrim.pipelines.voice import KokoroClient, VoicePipeline
from pilgrim.producer.gates import (
    PriorityGate,
    SlotGate,
    SongJob,
)
from pilgrim.store import Store

log = logging.getLogger("radio.producer")


class ProducerBase:
    def __init__(self, cfg: Config, store: Store, llm: LLM, kokoro: KokoroClient,
                 voice: VoicePipeline, songs: SongPipeline, clock: Clock,
                 prompts: dict[str, str], media_dir: Path, api_key: str, rng: RNG,
                 db=None, news_pipeline: NewsPipeline | None = None,
                 clock_time=None, sfx: SfxPipeline | None = None,
                 idle: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.kokoro = kokoro
        self.voice = voice
        self.songs = songs
        self.clock = clock
        self.prompts = prompts
        self._pen_until_ts = 0.0  # newest chat message a field report covered
        self.media_dir = media_dir
        self.rng = rng
        self.api_key = api_key
        self.news = news_pipeline or NewsPipeline(cfg, llm, prompts=prompts)
        # real local time for DJ talk; injectable in tests (OVERHAUL 5.3)
        self._clock_time = clock_time or current_local_time
        self._news_ok: dict | None = None
        self.sfx = sfx  # None = station airs dry (no SFX backend configured)
        # Set by server.py after the ArtWorker exists. Early art is optional:
        # tests and art-disabled stations leave it None.
        self.art_worker: Any | None = None
        # clock.now() when the last stock song finished (None = none yet this run)
        self._last_stock_song_at: float | None = None
        # one-shot log so a capped stock worker doesn't spam every 20 s
        self._stock_cap_logged = False
        # docs/shipped/idle.md: station policy predicate (no listeners -> idle). Default
        # never idle so tests, seed and the sim behave exactly as before.
        self.idle: Callable[[], bool] = idle or (lambda: False)
        self._idle_stock_logged = False
        # song jobs in flight under song_loop (RADIO §6.1): task -> job
        self._jobs: dict[asyncio.Task, SongJob] = {}
        # song briefs, at most `models.max_concurrency[brief model]` at a time
        # (requests and stock): one brief was the historical ceiling (2026-11-14
        # raised it to 3 now that calls stream — the read timeout measures
        # inactivity between chunks, not total wall time); each brief still
        # sees the genres of the jobs still rendering
        self._brief_gate = PriorityGate(
            self.cfg.models.max_concurrency.get(self.cfg.models.briefs, 1))
        # generator slots, granted by queue order: a job waits here after its
        # brief, so the words are written while another song renders (§6.1)
        self._render_gate = SlotGate(self._capacity, self._paid_briefing,
                                     self._free_render_caps)
        # set when a job's render returns: the song loop starts the next job
        # at once instead of on its 20 s tick
        self._render_done = asyncio.Event()
        # request_id -> cut-off briefs so far; the first few are free retries
        self._llm_cuts: dict[int, int] = {}
        # clock buckets whose render QC rejected: not retried (ensure_time_checks)
        self._time_check_rejected: set[int] = set()
        # songs whose code-written intro failed to render (ensure_song_intros)
        self._intro_rejected: set[int] = set()
        # station-clock times before which the slow housekeeping checks rest
        self._intro_check_after = 0.0
        self._turnover_check_after = 0.0
        # injectable wait for song_loop: tests drive it without wall time
        self._sleep = asyncio.sleep
        # request_id -> "waiting" (its turn at the brief LLM) | "writing"
        # (brief/lyrics) | "generating" (song model, then intro); only for
        # `producing` rows. In memory on purpose: a
        # restart re-queues producing requests anyway (RADIO §11 board stage)
        self.request_stage: dict[int, str] = {}

    def _count_fresh_unexpired(self, type_: str) -> int:
        """Unaired clips the scheduler can still air: expired time-mention DJ
        clips (5.3) and stale bulletins are not stock."""
        now = self.clock.wall()
        n = 0
        for it in self.store.list_items(type_):
            if not it["fresh"] or it["emergency"]:
                continue
            exp = it.get("expires_at")
            try:
                if exp and datetime.fromisoformat(exp).timestamp() <= now:
                    continue
            except ValueError:
                pass
            n += 1
        return n

    def _not_expired(self, item: dict) -> bool:
        exp = item.get("expires_at")
        if not exp:
            return True
        try:
            return datetime.fromisoformat(exp).timestamp() > self.clock.wall()
        except Exception:
            return True

    def _store_voice(self, item: dict, type_: str, evergreen: bool = True,
                     expires_at: str | None = None, gravity: str | None = None) -> None:
        """Store a rendered voice clip right away, airable dry. Its script cues
        stay in meta as `sfx_pending` for the SFX worker (sfx_step): talk never
        waits on the SFX backend."""
        meta = item.setdefault("meta", {})
        wants = bool(meta.get("sfx_cues") or meta.get("joke_after_sentence") is not None
                     or type_ == "field_report")
        if wants and self.sfx is not None and self.cfg.sfx.enabled \
                and type_ in SFX_HOST_TYPES:
            meta["sfx_pending"] = True
        else:
            meta.pop("sfx_cues", None)
            meta.pop("joke_after_sentence", None)
        self.store.add_item(
            type_=type_, media_path=item["media_path"], duration_s=item["duration_s"],
            sample_rate=item.get("sample_rate"), channels=item.get("channels"),
            role=item.get("role"), evergreen=evergreen, fresh=True,
            expires_at=expires_at, gravity=gravity, meta=item.get("meta"))

    def _capacity(self) -> int:
        """Concurrent song jobs allowed now: the generator pool's healthy
        slots (1 for a pipeline without a pool)."""
        cap = getattr(self.songs, "capacity", None)
        return max(0, int(cap())) if callable(cap) else 1

    def _free_render_caps(self) -> tuple[int, int]:
        """`(free work, free requests)` that may render at once: free requests
        and stock never fill every slot (one stays open for a paid request;
        with a single slot there is nothing to reserve), and at most
        songs.free_request_slots free requests render together (RADIO §6.1)."""
        cap = self._capacity()
        total = cap - 1 if cap > 1 else cap
        return total, min(total, self.cfg.songs.free_request_slots)

    def _paid_briefing(self) -> bool:
        """A paid request is still writing its brief: free work waits at the
        render gate so the paid song is next (RADIO §6.1)."""
        return any(j.paid and j.phase == "brief" for j in self._jobs.values())
