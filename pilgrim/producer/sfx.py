"""Sound effects: the stock pool and attaching cues to voice items (RADIO §6.1).
"""
from __future__ import annotations

import json
import logging

from pilgrim.logging_setup import err_text
from pilgrim.pipelines.sfx import SFX_HOST_TYPES, beat_offset
from pilgrim.producer.base import ProducerBase

log = logging.getLogger("radio.producer")


class SfxProducer(ProducerBase):
    def sfx_stock_target(self) -> int:
        """Approved evergreen cues: the stock stinger pool's target size."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        return sum(1 for c in self.cfg.sfx.cues.values() if c.evergreen and c.approved)

    def count_sfx_stock(self) -> int:
        approved = {n for n, c in self.cfg.sfx.cues.items() if c.evergreen and c.approved}
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        return len({self._sfx_cue(it) for it in self.store.list_items("sfx")
                    if it["evergreen"]} & approved)

    # ------------------------------------------------------------------ sfx
    async def ensure_sfx_pool(self, limit: int | None = None) -> int:
        """Render each approved evergreen cue once into the recycled stock pool
        (docs/systems/sfx.md §6.2). Contextual cues are rendered per host in _attach_sfx.
        Returns how many were made; a failing backend just leaves gaps."""
        if self.sfx is None or not self.cfg.sfx.enabled:
            return 0
        have = {self._sfx_cue(it) for it in self.store.list_items("sfx")}
        made = 0
        for name, cue in self.cfg.sfx.cues.items():
            if not (cue.evergreen and cue.approved) or name in have:
                continue
            if limit is not None and made >= limit:
                break
            try:
                r = await self.sfx.render(name)
            except Exception as e:
                log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
                return made
            self._store_sfx(r, evergreen=True)
            made += 1
        return made

    @staticmethod
    def _sfx_cue(it: dict) -> str | None:
        try:
            return (json.loads(it.get("meta_json") or "{}") or {}).get("cue")
        except ValueError:
            return None

    def _stock_sfx(self, name: str) -> dict | None:
        for it in self.store.list_items("sfx"):
            if it["evergreen"] and self._sfx_cue(it) == name:
                return it
        return None

    def _store_sfx(self, r: dict, evergreen: bool) -> int:
        return self.store.add_item(
            type_="sfx", media_path=r["media_path"], duration_s=r["duration_s"],
            sample_rate=r.get("sample_rate"), channels=r.get("channels"),
            role="sfx", evergreen=evergreen, fresh=True, meta=r.get("meta"))

    async def _attach_sfx(self, item: dict, role: str) -> None:
        """Resolve a rendered host's script cues to stored sfx items and record
        them as `meta.sfx` overlays (docs/systems/sfx.md §7.1). Which of them air (coin flip,
        rate budget) is decided at commit time by sfx_plan. Never fatal: any
        cue that can't be rendered is dropped and the host airs dry."""
        meta = item.setdefault("meta", {})
        cues = list(meta.pop("sfx_cues", None) or [])
        joke = meta.pop("joke_after_sentence", None)
        sfx = self.sfx
        if sfx is None or not self.cfg.sfx.enabled or role not in SFX_HOST_TYPES:
            return
        text = str(meta.get("text") or "")
        dur = float(item["duration_s"])
        lead = self.cfg.audio.edge_pad_ms / 1000.0
        sfx_cfg = self.cfg.sfx
        overlays: list[dict] = []

        async def resolve(name: str, **kw) -> dict | None:
            cue = sfx_cfg.cues.get(name)
            if not cue or not cue.approved:
                return None
            if cue.evergreen:
                # stock cue not rendered yet: make it now (once, for everyone)
                if self._stock_sfx(name) is None:
                    try:
                        self._store_sfx(await sfx.render(name), evergreen=True)
                    except Exception as e:
                        log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
                return self._stock_sfx(name)
            try:
                r = await sfx.render(name, seed=self.rng.randint(0, 2**31 - 1), **kw)
            except Exception as e:
                log.warning("sfx.failed", extra={"cue": name, "error": err_text(e)})
                return None
            sid = self._store_sfx(r, evergreen=False)
            return {"id": sid, "duration_s": r["duration_s"]}

        wanted = [(c["cue"], "stinger", c["after_sentence"]) for c in cues]
        if joke is not None:
            wanted.append((sfx_cfg.joke_cue, "joke", joke))
        for name, kind, after in wanted:
            got = await resolve(name)
            if not got:
                continue
            # a beat after the last sentence lands at the clip's end, where
            # the hit could never fit: pull it in so it ends with the clip
            off = min(beat_offset(text, after, dur, lead),
                      round(dur - got["duration_s"] - 0.05, 3))
            if off < lead:
                continue  # clip too short for this hit
            overlays.append({"item_id": got["id"], "cue": name, "kind": kind,
                             "offset_s": off, "duration_s": got["duration_s"]})
        if role == "field_report":
            bed_s = min(sfx_cfg.bed_max_s, dur - lead)
            if bed_s >= 1.0:
                got = await resolve(sfx_cfg.bed_cue, duration_s=bed_s)
                if got:
                    overlays.append({"item_id": got["id"], "cue": sfx_cfg.bed_cue,
                                     "kind": "bed", "offset_s": 0.0,
                                     "duration_s": got["duration_s"]})
        if overlays:
            meta["sfx"] = overlays
