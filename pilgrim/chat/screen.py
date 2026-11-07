"""Layer A: deterministic screening before a chat message is accepted.

SHEEP_PEN.md §6 Layer A. Runs synchronously on the web tier, costs nothing,
and rejects before anything is written: empty, overlength, control/invisible
characters, links/contact info (the station's existing prefilter), prohibited
terms (digest list), and repeats. Swear words are deliberately **not** filtered,
flagged or masked (§0.6.3); there is no profanity list and no language flag.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from pilgrim.chat import prohibited
from pilgrim.config import Chat
from pilgrim.pipelines.request_filter import prefilter

DETAILS = {
    "empty": "Say something first.",
    "too_long": "That's a bit long for the pen.",
    "bad_chars": "That message has characters the pen can't show.",
    "link": "No links in the pen.",
    "prohibited": "That word isn't welcome here.",
    "repeat": "You just said that.",
}


@dataclass
class ScreenResult:
    ok: bool
    code: str | None = None
    detail: str = ""
    text: str = ""
    flags: list[str] = field(default_factory=list)

    @classmethod
    def reject(cls, code: str) -> ScreenResult:
        return cls(False, code, DETAILS.get(code, "That message can't be sent."))


def screen_text(raw: str, cfg: Chat, *, repeated: bool = False,
                name_recent: bool = False) -> ScreenResult:
    """Layer A on the trimmed text. `repeated` is the caller's repeat check;
    `name_recent` marks a recently renamed author for Layer B's attention."""
    text = raw.strip()
    if not text:
        return ScreenResult.reject("empty")
    if len(text) > cfg.max_length:
        return ScreenResult.reject("too_long")
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        return ScreenResult.reject("bad_chars")
    if "<" in text or ">" in text:   # HTML/script injection: plain text only
        return ScreenResult.reject("bad_chars")
    if prefilter(text):
        return ScreenResult.reject("link")
    if prohibited.prohibited_hit(text):
        return ScreenResult.reject("prohibited")
    if repeated:
        return ScreenResult.reject("repeat")

    flags: list[str] = []
    if len(text) >= int(cfg.max_length * 0.9):
        flags.append("near_limit_rate")
    if name_recent:
        flags.append("name_recent")
    return ScreenResult(True, text=text, flags=flags)
