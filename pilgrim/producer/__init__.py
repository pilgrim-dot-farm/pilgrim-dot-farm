"""Producer (RADIO.md §6): demand-driven inventory refill.

Loops keep inventory at low-water targets using real pipelines. Slow work
(songs on the generator pool) runs in its own background task so voice
inventory never stalls; up to the pool's capacity song jobs run concurrently.
Playout never waits on production: the scheduler only commits rendered items.
"""
from __future__ import annotations

import json
import logging

from pilgrim.logging_setup import err_text
from pilgrim.pipelines.sfx import SFX_HOST_TYPES
from pilgrim.producer.gates import (
    LLM_FREE_CUTS,
    STOCK_BRIEF_KEY,
    PriorityGate,
    SlotGate,
    SongJob,
)
from pilgrim.producer.songs import SongProducer

log = logging.getLogger("radio.producer")

__all__ = ["LLM_FREE_CUTS", "STOCK_BRIEF_KEY", "PriorityGate", "Producer", "SlotGate", "SongJob"]


class Producer(SongProducer):
    """The station's producer: inventory counts and the background loops. The
    content types live in the modules this class is built from (`talk`,
    `promos`, `sfx`, `songs`); shared state is in `base`."""

    # --------------------------------------------------------------- counts
    def counts(self) -> dict[str, int]:
        # Evergreen types (song/commercial/liner) recycle: keep a usable
        # low-water stock. dj_talk is contextual and NEVER recycled (AGENTS rule
        # 10), so it only counts unaired clips (OVERHAUL 5.1). News is expiring.
        # This is THE inventory count: /api/health reports it and the ensure_*
        # loops refill against it, so the site and the producer never disagree.
        return {
            "song": self.store.count_fresh_of_type("song"),
            "commercial": self.store.count_usable_of_type("commercial"),
            "liner": self.store.count_usable_of_type("liner"),
            "dj_talk": self._count_fresh_unexpired("dj_talk"),
            "field_report": self._count_fresh_unexpired("field_report"),
            "news": self._count_fresh_unexpired("news"),
            "sfx": self.count_sfx_stock(),
            "promo": self._count_promos(now_wall=self.clock.wall()),
        }

    # --------------------------------------------------------------- loops
    async def run(self) -> None:
        log.info("producer running")
        while True:
            if self.idle():
                # docs/shipped/idle.md D6: nobody is listening, so voice refill pauses.
                # Listener requests keep producing (song_loop) — a requester
                # is on the page waiting; a paid one has paid.
                await self._sleep(self.cfg.idle.producer_poll_s)
                continue
            try:
                # words between songs first (§5.2): station IDs, liners, DJ
                # talk; SFX render in their own worker (sfx_loop)
                await self.ensure_station_ids()
                await self.ensure_liners()
                await self.ensure_dj()
                await self.ensure_time_checks()
                await self.ensure_promos()
                await self.ensure_commercials()
                await self.ensure_field_reports()
                self.ensure_turnover()
                await self.ensure_song_intros()
            except Exception as e:
                log.warning("producer.voice_cycle_failed: %s", e)
            await self._sleep(6.0)

    async def sfx_loop(self) -> None:
        """SFX worker (docs/systems/sfx.md §7.3): attaches overlays to stored talk, then
        fills the evergreen stock pool. One render at a time; the backend is
        slow (docs/backends.md §7) and nothing waits on it."""
        while True:
            if self.idle():
                await self._sleep(self.cfg.idle.producer_poll_s)
                continue
            try:
                did = await self.sfx_step()
            except Exception as e:
                log.warning("sfx.loop_failed: %s", err_text(e))
                did = False
            await self._sleep(1.0 if did else 5.0)

    async def sfx_step(self) -> bool:
        """One unit of SFX work. Returns True if it did any.
        1) the oldest unaired talk clip with pending cues gets its overlays
           (aired evergreen clips after that: they air again);
        2) otherwise one missing evergreen stock cue is rendered."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return False
        pending: list[dict] = []
        for t in sorted(SFX_HOST_TYPES):
            for it in self.store.list_items(t):
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
                if meta.get("sfx_pending"):
                    pending.append({**it, "meta": meta})
        if pending:
            it = min(pending, key=lambda i: (not i["fresh"], i["id"]))
            item = {"duration_s": it["duration_s"], "meta": it["meta"]}
            await self._attach_sfx(item, it["type"])
            item["meta"].pop("sfx_pending", None)
            self.store.update_item_meta(it["id"], item["meta"])
            return True
        return await self.ensure_sfx_pool(limit=1) > 0

    async def news_loop(self) -> None:
        while True:
            if self.idle():
                await self._sleep(self.cfg.idle.producer_poll_s)
                continue
            try:
                await self.ensure_news()
            except Exception as e:
                log.warning("news.loop_failed: %s", e)
            await self._sleep(5.0)
