"""Campaign promos and the station's self-promotion spots (RADIO §6.1).
"""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pilgrim.logging_setup import err_text
from pilgrim.producer.talk import TalkProducer
from pilgrim.promotions import (
    active_campaign,
    days_until,
    fill_day_tokens,
    next_local_midnight,
    phrase_for,
    target_per_hour,
    validate_promo_copy,
    validate_promo_template,
)

log = logging.getLogger("radio.producer")


class PromoProducer(TalkProducer):
    # --------------------------------------------------------------- promos
    def _count_promos(self, campaign_id: str | None = None,
                      kind: str | None = None,
                      now_wall: float | None = None) -> int:
        """Ready promos (recyclable), optionally filtered by campaign or kind.
        Day-level promos expire at local midnight, so they are not stock after
        that even though the row survives (revision 6)."""
        n = 0
        for it in self.store.list_items("promo"):
            if it["emergency"]:
                continue
            if now_wall is not None:
                exp = it.get("expires_at")
                if exp:
                    try:
                        if datetime.fromisoformat(exp).timestamp() <= now_wall:
                            continue
                    except ValueError:
                        pass
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            if campaign_id is not None and meta.get("campaign_id") != campaign_id:
                continue
            if kind is not None and meta.get("kind") != kind:
                continue
            n += 1
        return n

    def _retire_finished_promos(self, now_wall: float, tz: str) -> None:
        """A campaign's promos are retired (not deleted) once its ramp ends, so
        nothing recycles into next year (ANNOUNCER_OVERHAUL C5)."""
        for it in self.store.list_items("promo"):
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            cid = meta.get("campaign_id")
            if not cid:
                continue
            camp = next((c for c in self.cfg.promotions.campaigns if c.id == cid), None)
            expired = False
            exp = it.get("expires_at")
            if exp:
                with contextlib.suppress(ValueError):
                    expired = datetime.fromisoformat(exp).timestamp() <= now_wall
            if expired or camp is None or target_per_hour(now_wall, tz, camp) <= 0:
                self.store.retire_item(it["id"], "campaign promo expired/ended")

    async def ensure_promos(self) -> None:
        """Keep ready promos per source. Campaign promos only while the ramp
        runs; the steady kinds (request line, paid tier, Tree Fiddy Report) are
        folded into the normal promo mix so the station advertises itself all
        day instead of in bursts (operator: "advertise steadily")."""
        cfg = self.cfg
        tz = cfg.station.timezone
        now = self.clock.wall()
        camp = active_campaign(now, tz, cfg.promotions.campaigns)
        self._retire_finished_promos(now, tz)  # expired day-level promos too
        jobs: list[tuple[str, int, Any, Any]] = []
        if camp is not None:
            jobs.append((
                f"campaign:{camp.id}", cfg.inventory.promos_min,
                lambda c=camp: self._make_campaign_promo(c),
                lambda c=camp: self._count_promos(campaign_id=c.id, now_wall=now)))
        jobs += [
            ("request_line", 2, self._make_request_line,
             lambda: self._count_promos(kind="request_line", now_wall=now)),
            ("paid_tier", 2, self._make_paid_tier,
             lambda: self._count_promos(kind="paid_tier", now_wall=now)),
            ("tree_fiddy", 1, self._make_tree_fiddy,
             lambda: self._count_promos(kind="tree_fiddy", now_wall=now)),
        ]
        for name, target, maker, counter in jobs:
            have = counter()
            need = target - have
            log.debug("producer.need", extra={
                "item_type": f"promo/{name}", "have": have, "target": target})
            for _ in range(max(0, need)):
                try:
                    if await maker() is None:
                        break
                except Exception as e:
                    log.warning("promo.production_failed: %s: %s", name, err_text(e))
                    break

    async def _make_campaign_promo(self, camp) -> dict | None:
        """LLM-written campaign promo; the code fills the day tokens and
        rejects wrong claims (revision 5/6)."""
        cfg = self.cfg
        tz = cfg.station.timezone
        now = self.clock.wall()
        days = days_until(now, tz, camp)
        phrase = phrase_for(days)
        ctx = (f"Event: {camp.label}. Date: {camp.date}. CTA: {camp.cta}.\n"
               f"Timing phrase: {phrase}. Write it exactly, or use the "
               f"[[days_until]] token.\n"
               f"Angles you may use: {'; '.join(camp.lines)}")
        host = self._current_host()
        copy = await self.voice.write_copy(
            "promo", 12.0, context=ctx, host=host.name if host else "Liam")
        reason = validate_promo_template(copy["text"], camp, cfg.station.name,
                                         days)
        if reason:
            # one retry with the rejection fed back, then give up rather
            # than air a wrong claim
            copy = await self.voice.write_copy(
                "promo", 12.0,
                context=ctx + f"\nYour previous attempt was rejected: "
                              f"{reason}. Fix exactly that and keep "
                              f"[[event_name]].",
                host=host.name if host else "Liam")
            reason = validate_promo_template(copy["text"], camp,
                                             cfg.station.name, days)
            if reason:
                log.warning("promo.rejected", extra={"reason": reason})
                return None
        copy["text"] = fill_day_tokens(copy["text"], now, tz, camp)
        reason = validate_promo_copy(copy["text"], camp, cfg.station.name)
        if reason:
            log.warning("promo.rejected", extra={"reason": reason})
            return None
        copy["evergreen"] = True
        rendered = await self.voice.render(
            copy, "promo", 12.0, voice=host.voice if host else None)
        item = self.voice.item_dict("promo", copy, rendered)
        item["meta"]["kind"] = "campaign"
        item["meta"]["campaign_id"] = camp.id
        expires = datetime.fromtimestamp(
            next_local_midnight(now, tz), UTC).isoformat()
        self._store_voice(item, "promo", evergreen=True, expires_at=expires)
        log.info("produced campaign promo %.1fs", item["duration_s"])
        return item

    async def _make_request_line(self) -> dict | None:
        lines = self.cfg.promotions.request_line_lines
        if not lines:
            return None
        text = lines[self.rng.randint(0, len(lines) - 1)]
        host = self._current_host()
        rendered = await self.voice.render(
            {"text": text}, "promo", 8.0, voice=host.voice if host else None)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": True}, rendered)
        item["meta"]["kind"] = "request_line"
        self._store_voice(item, "promo", evergreen=True)
        log.info("produced request_line promo %.1fs", item["duration_s"])
        return item

    async def _make_paid_tier(self) -> dict | None:
        """Steady paid-tier promo: code-written, copy only, no payment code
        (AGENTS rule 11)."""
        lines = self.cfg.promotions.paid_tier_lines
        if not lines:
            return None
        text = lines[self.rng.randint(0, len(lines) - 1)]
        host = self._current_host()
        rendered = await self.voice.render(
            {"text": text}, "promo", 14.0, voice=host.voice if host else None)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": True}, rendered)
        item["meta"]["kind"] = "paid_tier"
        self._store_voice(item, "promo", evergreen=True)
        log.info("produced paid_tier promo %.1fs", item["duration_s"])
        return item

    async def _make_tree_fiddy(self) -> dict | None:
        """The administrator's weekly count, in the operator character's voice
        (am_justin). Real numbers from the requests table, regenerated hourly
        so the count is current; never names or emails."""
        cfg = self.cfg
        now = self.clock.wall()
        since = datetime.fromtimestamp(now - 7 * 86400, UTC).isoformat()
        n = self.store.count_paid_aired_since(since)
        if n:
            text = cfg.promotions.tree_fiddy_lines[0].format(
                n=n, s="" if n == 1 else "s")
        else:
            text = cfg.promotions.tree_fiddy_none
        voice = cfg.dj.character.voice or cfg.voices.dj
        rendered = await self.voice.render(
            {"text": text}, "promo", 20.0, voice=voice)
        item = self.voice.item_dict(
            "promo", {"text": text, "evergreen": False}, rendered)
        item["meta"]["kind"] = "tree_fiddy"
        item["meta"]["paid_aired_7d"] = n
        expires = datetime.fromtimestamp(
            now + cfg.promotions.tree_fiddy_gap_s, UTC).isoformat()
        self._store_voice(item, "promo", evergreen=False, expires_at=expires)
        log.info("produced tree_fiddy report (%d paid aired in 7d)", n)
        return item
