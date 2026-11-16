"""Voice inventory (RADIO §6.1): liners, IDs, commercials, DJ talk, field
reports, time checks, news."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pilgrim import genres
from pilgrim.logging_setup import err_text
from pilgrim.pipelines.clocktime import (
    mentions_clock,
    mentions_day,
    spoken_time,
)
from pilgrim.producer.gates import (
    SLOW_CHECK_S,
    STATION_ID_BRIEF,
    _created_epoch,
)
from pilgrim.producer.sfx import SfxProducer
from pilgrim.promotions import (
    next_local_midnight,
)

log = logging.getLogger("radio.producer")


class TalkProducer(SfxProducer):
    # ------------------------------------------------------------- producers
    async def ensure_liners(self) -> None:
        """Fill liners toward a TOTAL target (liners_per_bucket * #buckets) by
        cycling the target duration through the configured buckets. The old
        per-bucket filling looped forever when rendered durations never landed
        in the target bucket (OVERHAUL 2.3). At most 3 items per call."""
        inv = self.cfg.inventory
        target_total = inv.liners_per_bucket * len(inv.liner_buckets_s)
        usable = self.store.count_usable_of_type("liner")
        log.debug("producer.need", extra={
            "item_type": "liner", "have": usable, "target": target_total})
        if usable >= target_total:
            return
        made = 0
        idx = 0
        buckets = list(inv.liner_buckets_s) or [5]
        # A bucket whose copy overshoots its duration tolerance (the 3 s bucket
        # renders ~9 s) must not pin the loop forever: try each bucket once per
        # call and move on.
        while made < 3 and usable < target_total and idx < len(buckets):
            bucket = float(buckets[idx % len(buckets)])
            idx += 1
            try:
                item = await self.voice.produce_item("liner", bucket)
                self._store_voice(item, "liner")
                log.info("produced liner %.1fs", item["duration_s"])
                made += 1
                usable += 1
            except Exception as e:
                log.warning("liner.production_failed: %.0fs bucket: %s",
                            bucket, err_text(e))
                continue

    async def ensure_station_ids(self) -> None:
        """Keep `inventory.station_ids_min` emergency station IDs (RADIO §8.3).
        They are the floor under 'words between songs' (§5.2): rendered once,
        never deleted, aired only when no other liner exists."""
        have = sum(1 for i in self.store.list_items("station_id") if i["emergency"])
        for _ in range(max(0, self.cfg.inventory.station_ids_min - have)):
            try:
                item = await self.voice.produce_item("liner", 4.0, context=STATION_ID_BRIEF)
            except Exception as e:
                log.warning("station_id.production_failed: %s", err_text(e))
                return
            meta = {"text": (item.get("meta") or {}).get("text")}
            self.store.add_item(
                type_="station_id", media_path=item["media_path"],
                duration_s=item["duration_s"], sample_rate=item.get("sample_rate"),
                channels=item.get("channels"), role="liner", evergreen=True,
                emergency=True, fresh=True, meta=meta)
            log.info("produced station_id %.1fs", item["duration_s"])

    async def ensure_commercials(self) -> None:
        have = self.store.count_usable_of_type("commercial")
        need = self.cfg.inventory.commercials_min - have
        log.debug("producer.need", extra={
            "item_type": "commercial", "have": have,
            "target": self.cfg.inventory.commercials_min})
        if need <= 0:
            return
        role = "commercial"
        for _ in range(min(need, 3)):  # a few per cycle: callouts come first
            try:
                item = await self.voice.produce_item(
                    role, float(self.cfg.inventory.commercial_target_s))
                self._store_voice(item, role)
                log.info("produced commercial %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("commercial.production_failed: %s", err_text(e))
                return

    async def ensure_dj(self) -> None:
        # dj_talk is contextual: only unaired clips are stock (AGENTS rule 10,
        # OVERHAUL 5.1) — aired DJ clips are gone, so we keep topping up.
        have = self._count_fresh_unexpired("dj_talk")
        need = self.cfg.inventory.dj_talk_min - have
        log.debug("producer.need", extra={
            "item_type": "dj_talk", "have": have,
            "target": self.cfg.inventory.dj_talk_min})
        if need <= 0:
            return
        # Material to talk about: the songs that just played (chosen NOW, so
        # no fake 'next song' claims), code-computed day-level facts (revision
        # 6), a weather phrase from config, and a rotated topic. The CLOCK is
        # deliberately absent: time_check carries it (A4), so the DJ stops
        # reading the time on every break.
        recent = self._recent_committed_songs(3)
        tz = self.cfg.station.timezone
        now_wall = self.clock.wall()
        now_dt = datetime.fromtimestamp(now_wall, ZoneInfo(tz))
        context_parts: list[str] = []
        if recent:
            context_parts.append("Songs that played recently: " + "; ".join(
                f'"{t}" by {a} ({genres.display(g)})' for t, a, g in recent))
        context_parts.append(
            f"Day: {now_dt.strftime('%A')}, {now_dt.strftime('%B')} {now_dt.day}.")
        context_parts.append(f"Day part: {self._day_part(now_dt.hour)}.")
        weather = self._weather_phrase()
        if weather:
            context_parts.append(f"Thistledown weather: {weather}.")
        topic = self._pick_topic()
        if topic:
            context_parts.append(f"Topic: {topic}.")
        host = self._current_host()
        host_name = host.name if host else "Liam"
        host_voice = host.voice if host else None
        if host and host.persona:
            context_parts.append(f"Host persona: {host.persona}.")
        context_parts.extend(self._request_context())
        context = "\n".join(context_parts)
        # Expiry is stamped AFTER the LLM call (revision 3). A clock/day-part
        # statement expires on the short TTL; a weekday/date fact expires at
        # local midnight (revision 6); copy with neither is not perishable and
        # may wait on the shelf.
        for _ in range(max(0, need)):
            try:
                copy = await self.voice.write_copy(
                    "dj_talk", 18.0, context=context, host=host_name)
                reason = self._motif_reason(copy["text"])
                if reason:
                    copy = await self.voice.write_copy(
                        "dj_talk", 18.0,
                        context=context + f"\nDo not use the word/phrase "
                                          f"'{reason}' this time.",
                        host=host_name)
                    reason = self._motif_reason(copy["text"])
                    if reason:
                        log.warning("talk.motif_reuse", extra={"motif": reason})
                rendered = await self.voice.render(copy, "dj_talk", 18.0,
                                                   voice=host_voice)
                item = self.voice.item_dict("dj_talk", copy, rendered)
                text = copy["text"]
                wall = self.clock.wall()  # after the LLM call, not before
                expires = None
                if mentions_clock(text):
                    expires = (datetime.fromtimestamp(wall, UTC) + timedelta(
                        seconds=self.cfg.talk.time_mention_ttl_s)).isoformat()
                elif mentions_day(text):
                    expires = datetime.fromtimestamp(
                        next_local_midnight(wall, tz), UTC).isoformat()
                item["meta"]["topic"] = topic
                item["meta"]["motifs"] = [
                    m for m in self.cfg.talk.motif_bank if m.lower() in text.lower()]
                self._store_voice(item, "dj_talk", evergreen=True,
                                  expires_at=expires)
                log.info("produced dj_talk %.1fs", item["duration_s"])
            except Exception as e:
                log.warning("dj_talk.production_failed: %s", err_text(e))
                return

    def _day_part(self, hour: int) -> str:
        if 5 <= hour < 11:
            return "morning drive"
        if 11 <= hour < 14:
            return "midday"
        if 14 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "overnight"

    def _current_host(self):
        """The host on shift now (by day part); None when hosts are not
        configured (single-host station)."""
        hosts = self.cfg.talk.hosts
        if not hosts:
            return None
        hour = datetime.fromtimestamp(
            self.clock.wall(), ZoneInfo(self.cfg.station.timezone)).hour
        part = self._day_part(hour)
        for h in hosts:
            if not h.day_parts or part in h.day_parts:
                return h
        return hosts[0]

    def _weather_phrase(self) -> str:
        phrases = self.cfg.talk.weather_phrases
        return phrases[self.rng.randint(0, len(phrases) - 1)] if phrases else ""

    def _recent_dj_meta(self, n: int) -> list[dict]:
        """meta of the newest n fresh dj_talk clips (for topic/motif rotation)."""
        out: list[dict] = []
        for it in reversed(self.store.list_items("dj_talk")):
            if not it["fresh"] or it["emergency"]:
                continue
            try:
                out.append(json.loads(it["meta_json"]) if it.get("meta_json") else {})
            except ValueError:
                out.append({})
            if len(out) >= n:
                break
        return out

    def _pick_topic(self) -> str:
        topics = self.cfg.talk.topic_bank
        if not topics:
            return ""
        recent = {m.get("topic") for m in self._recent_dj_meta(8)}
        candidates = [t for t in topics if t not in recent] or topics
        return candidates[self.rng.randint(0, len(candidates) - 1)]

    def _request_context(self) -> list[str]:
        """Request back-announce / tease (revision 8). Copy only: never a name
        or email (AGENTS rule 11 / PAYMENT §0.3)."""
        out: list[str] = []
        ready = self.store.ready_request_songs()
        if ready:
            out.append(f'A listener request is coming up: "{ready[0]["text"]}".')
        since = datetime.fromtimestamp(self.clock.wall() - 1800, UTC).isoformat()
        recent = self.store.recently_aired_request(since)
        if recent:
            out.append(f'You just played a listener request: "{recent["text"]}".')
        return out

    def _motif_reason(self, text: str) -> str | None:
        """A banked motif that aired inside the gap, or None (revision 10)."""
        recent: set[str] = set()
        for m in self._recent_dj_meta(self.cfg.talk.motif_gap_items):
            recent.update(w.lower() for w in (m.get("motifs") or []))
        low = text.lower()
        for motif in self.cfg.talk.motif_bank:
            if motif.lower() in low and motif.lower() in recent:
                return motif
        return None

    async def ensure_field_reports(self) -> None:
        """Keep `field_reports_min` unaired field reports (docs/systems/sfx.md §4.2). Like
        dj_talk they are contextual: aired once, never recycled."""
        have = self._count_fresh_unexpired("field_report")
        need = self.cfg.inventory.field_reports_min - have
        log.debug("producer.need", extra={
            "item_type": "field_report", "have": have,
            "target": self.cfg.inventory.field_reports_min})
        for _ in range(max(0, need)):
            context, pen = "Do not mention the clock time.", None
            chatter = self._pen_chatter()
            if chatter:
                context, pen = chatter
            try:
                item = await self.voice.produce_item("field_report", 20.0, context=context)
                if pen:
                    item.setdefault("meta", {})["pen_chat"] = pen
                    self._pen_until_ts = pen["until_ts"]
                self._store_voice(item, "field_report", evergreen=False)
                log.info("produced field_report %.1fs", item["duration_s"], extra={
                    "item_type": "field_report", "pen_messages": pen["n"] if pen else 0})
            except Exception as e:
                log.warning("field_report.production_failed: %s", err_text(e))
                return

    def _pen_cursor(self) -> float:
        """Timestamp of the newest chat message a field report already covered:
        this process's last pen report, or the stored reports' meta after a
        restart."""
        cursor = self._pen_until_ts
        for it in self.store.list_items("field_report"):
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
                cursor = max(cursor, float((meta.get("pen_chat") or {}).get("until_ts") or 0.0))
            except (ValueError, TypeError, AttributeError):
                continue
        return cursor

    def _pen_chatter(self) -> tuple[str, dict[str, Any]] | None:
        """Context for a field report about the Sheep Pen chat (RADIO §6.1
        "Field reports and the pen"), or None for an ordinary report.

        Reads the durable chat copy only: listener messages the review worker
        has passed (`accepted`; never unreviewed, flagged or deleted), inside
        the window and newer than the last pen report. Text only — no names or ids reach the
        copy model. A failed read means an ordinary report, never an error."""
        talk = self.cfg.talk
        tmpl = self.prompts.get("field_pen") or ""
        if talk.field_pen_min_messages <= 0 or not self.cfg.chat.enabled or not tmpl:
            return None
        now = self.clock.wall()
        since = max(self._pen_cursor(), now - talk.field_pen_window_s)
        try:
            rows = self.store.recent_chat_messages(200)
        except Exception as e:  # noqa: BLE001 - chat is optional context
            log.warning("field_report.pen_read_failed: %s", err_text(e))
            return None
        fresh = [r for r in rows
                 if r.get("status") == "accepted"
                 and str(r.get("text") or "").strip()
                 and since < float(r.get("ts") or 0.0) <= now]
        if len(fresh) < talk.field_pen_min_messages:
            return None
        fresh.sort(key=lambda r: float(r["ts"]))
        picked = fresh[-talk.field_pen_max_messages:]
        lines = "\n".join("- " + " ".join(str(r["text"]).split()) for r in picked)
        return (tmpl.replace("{messages}", lines).strip(),
                {"n": len(picked), "until_ts": float(fresh[-1]["ts"])})

    # -------------------------------------------------------------- turnover
    def ensure_turnover(self) -> None:
        """Keep the evergreen pools changing (RADIO §6.1). For commercials and
        liners: when the pool is at its target and none was made for
        86400 / per_day seconds, retire the most-played item, so the next pass
        of ensure_commercials / ensure_liners writes a new one. Paced by the
        newest item's age, so it needs no state and survives restarts. Retired
        items keep their file (hard rule 5); they just stop airing."""
        inv = self.cfg.inventory
        if self.clock.now() < self._turnover_check_after:
            return
        self._turnover_check_after = self.clock.now() + SLOW_CHECK_S
        now = self.clock.wall()
        plans = (("commercial", inv.commercials_turnover_per_day, inv.commercials_min),
                 ("liner", inv.liners_turnover_per_day,
                  inv.liners_per_bucket * len(inv.liner_buckets_s)))
        for type_, per_day, target in plans:
            if per_day <= 0:
                continue
            pool = [i for i in self.store.list_items(type_)
                    if not i["retired"] and not i["emergency"]]
            if len(pool) < target or not pool:
                continue  # below target: production is already owed
            newest = max(_created_epoch(i) for i in pool)
            if now - newest < 86400.0 / per_day:
                continue
            worn = [i for i in pool
                    if int(i.get("play_count") or 0) >= inv.turnover_min_plays]
            if not worn:
                continue
            it = max(worn, key=lambda i: (int(i.get("play_count") or 0), -int(i["id"])))
            self.store.retire_item(it["id"], "turnover: most played")
            log.info("turnover.retired", extra={
                "item_type": type_, "item_id": it["id"],
                "plays": int(it.get("play_count") or 0)})

    # ----------------------------------------------------------- time checks
    def _time_check_text(self, bucket: float, tz: str) -> str:
        variants = self.cfg.talk.time_check_variants or ["It is {clock}."]
        clock = spoken_time(datetime.fromtimestamp(bucket, ZoneInfo(tz)))
        idx = int(bucket // 300) % len(variants)  # rotate deterministically
        return variants[idx].format(clock=clock)

    async def ensure_time_checks(self) -> None:
        """Render a code-written clock read for each 5-minute bucket in the
        next hour. The scheduler airs one only near a quarter mark (operator
        pick: top, quarter past, bottom, quarter til) and picks the variant
        whose bucket matches the ACTUAL slot, so the clock is never more than
        the 5-minute rounding off. Kokoro is ~0.2 s: a dozen variants an hour
        is nearly free, and each expires at the end of its bucket."""
        cfg = self.cfg
        if cfg.playout.breaks.enabled or not cfg.talk.time_check_marks_min:
            return  # the break structure airs no clock reads (RADIO §5.2)
        tz = cfg.station.timezone
        now = self.clock.wall()
        buckets = [int((now + k * 300) // 300 * 300) for k in range(12)]
        existing = set()
        for it in self.store.list_items("time_check"):
            if it["retired"]:
                continue
            exp = it.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp).timestamp() < now - 3600:
                        # stale renders would otherwise accumulate forever
                        self.store.retire_item(it["id"], "clock bucket passed")
                        continue
                except ValueError:
                    pass
            meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            existing.add(meta.get("bucket"))
        self._time_check_rejected &= set(buckets)   # forget buckets that passed
        for bucket in buckets:
            if bucket in existing or bucket in self._time_check_rejected:
                continue
            copy = {"text": self._time_check_text(float(bucket), tz),
                    "est_duration_s": 4.0, "evergreen": False}
            try:
                host = self._current_host()
                rendered = await self.voice.render(
                    copy, "time_check", 4.0, voice=host.voice if host else None)
            except ValueError as e:
                # QC rejected this exact text: the same words would fail the
                # same way on every tick, so skip the bucket and render the
                # others (2026-11-14: one bad bucket blocked the whole hour and
                # logged a warning every 8 s)
                self._time_check_rejected.add(bucket)
                log.warning("time_check.production_failed: %s", err_text(e))
                continue
            except Exception as e:
                # backend trouble: try again next tick
                log.warning("time_check.production_failed: %s", err_text(e))
                return
            item = self.voice.item_dict("time_check", copy, rendered)
            item["meta"]["bucket"] = bucket
            expires = datetime.fromtimestamp(bucket + 300, UTC).isoformat()
            self._store_voice(item, "time_check", evergreen=False,
                              expires_at=expires)
            log.info("produced time_check %.1fs", item["duration_s"])

    def _recent_committed_songs(self, n: int) -> list[tuple[str, str, str]]:
        """Last n songs in the committed program as (title, artist, genre)."""
        out: list[tuple[str, str, str]] = []
        for r in self.store.program_since(1):
            if r["type"] != "song":
                continue
            it = self.store.get_item(r["item_id"])
            if it:
                out.append((it.get("title") or "", it.get("artist") or "",
                            it.get("genre") or ""))
        return out[-n:] if n else []

    async def ensure_news(self) -> None:
        if not self.cfg.news.enabled:
            return
        latest = self.store.list_items("news")
        fresh_live = [i for i in latest if self._not_expired(i) and i["fresh"]]
        log.debug("producer.need", extra={
            "item_type": "news", "have": len(fresh_live), "target": 1})
        if latest and any(self._not_expired(i) for i in latest):
            fresh = [i for i in latest if i["fresh"]]
            if any(self._not_expired(i) for i in fresh):
                return  # a valid, unaired bulletin exists
        # need a fresh bulletin rendered as audio
        try:
            bulletin = await self.news.produce_bulletin()
            text = bulletin["text"]
            item = await self.voice.produce_item("news", 20.0, context=text)
            # injected clock, not datetime.now (AGENTS rule 3)
            expires = (datetime.fromtimestamp(self.clock.wall(), UTC)
                       + timedelta(seconds=self.cfg.news.refresh_s + 900))
            self._store_voice(item, "news", evergreen=False,
                              expires_at=expires.isoformat(), gravity=bulletin["gravity"])
            self._news_ok = bulletin
            log.info("produced news bulletin (gravity=%s)", bulletin["gravity"])
        except Exception as e:
            log.warning("news.production_failed: %s", err_text(e))
