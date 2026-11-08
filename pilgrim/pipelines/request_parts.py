"""The three-box paid request (PROMPT_UPGRADE.md Step 1).

A paid request arrives as up to three parts. They are stored separately and
also composed into one labelled document kept in `requests.text`, so every
consumer that reads the whole text (moderation, intro, admin, old station
processes) keeps working. Pure functions; no I/O.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LABELS = (("lyrics", "Lyrics"), ("music", "Music"), ("art", "Album art"))
_LABEL_LINE = re.compile(r"^(Lyrics|Music|Album art):$")


def normalise(text: str) -> str:
    """CRLF -> LF, rstrip lines, strip, 3+ blank lines -> 2 (same as
    server._normalise_paid, which should import this)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def is_label_line(line: str) -> bool:
    """True for a bare `Lyrics:` / `Music:` / `Album art:` line, so a fallback
    label never lands on the board."""
    return bool(_LABEL_LINE.match(" ".join(line.split())))


@dataclass(frozen=True)
class RequestParts:
    lyrics: str = ""
    music: str = ""
    art: str = ""

    @property
    def has_song(self) -> bool:
        """Both lyrics and musical direction are required (UI feedback
        2026-11-08); cover art is optional and never sufficient."""
        return bool(self.lyrics and self.music)

    def composed(self) -> str:
        """`Lyrics:\\n…\\n\\nMusic:\\n…\\n\\nAlbum art:\\n…`; empty parts omitted."""
        blocks = [f"{label}:\n{getattr(self, key)}"
                  for key, label in LABELS if getattr(self, key)]
        return "\n\n".join(blocks)

    def song_text(self) -> str:
        """Lyrics + music, labelled, never art: the copyright screen, the
        board summary and any brief fallback read this."""
        return RequestParts(self.lyrics, self.music, "").composed()

    def subject_text(self) -> str:
        """Unlabelled text for one-line fallbacks (board label): the lyrics
        box, else the music box."""
        return self.lyrics or self.music


def from_row(row: Mapping[str, Any] | None) -> RequestParts | None:
    """Parts stored on a `requests` row, or None for a legacy/free row
    (all three columns NULL)."""
    if not row:
        return None
    vals = tuple(row.get(k) for k in ("lyrics_text", "music_text", "art_text"))
    if all(v is None for v in vals):
        return None
    return RequestParts(*(str(v or "") for v in vals))
