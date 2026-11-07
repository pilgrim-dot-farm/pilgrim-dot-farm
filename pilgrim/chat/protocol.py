"""The Sheep Pen wire protocol (SHEEP_PEN.md §0.7.6; RADIO §11).

JSON text frames, one object each, `type` first. Client frames are parsed into
strict pydantic models (`extra="forbid"`); server frames are plain dicts built
by the helpers here so both the web tier and the e2e server emit exactly the
same shape.

No frame sent to a browser ever carries a user id, session/token hash, IP or
IP hash (SHEEP_PEN.md §0.7.6).
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

STREAM_ID_RE = re.compile(r"^\d+-\d+$")


def is_stream_id(value: object) -> bool:
    return isinstance(value, str) and bool(STREAM_ID_RE.fullmatch(value))


def stream_id_key(value: str) -> tuple[int, int]:
    """Sortable form of a Valkey stream id (`<ms>-<seq>`)."""
    ms, seq = value.split("-", 1)
    return int(ms), int(seq)


class Hello(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hello"]
    token: str = Field(min_length=32, max_length=32)
    last_id: str | None = None
    client_version: str | None = Field(default=None, max_length=40)


class NameFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["name"]
    name: str = Field(min_length=1, max_length=80)


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["send"]
    client_msg_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=2000)


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ping"]


class Pong(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["pong"]


ClientFrame = Hello | NameFrame | Send | Ping | Pong


def parse_client_frame(raw: str) -> ClientFrame:
    """Parse one client frame; raises `ValueError` on anything invalid."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("bad_json") from None
    if not isinstance(obj, dict):
        raise ValueError("bad_frame")
    kind = obj.get("type")
    models: dict[str, Any] = {
        "hello": Hello, "name": NameFrame, "send": Send, "ping": Ping,
        "pong": Pong,
    }
    model = models.get(kind if isinstance(kind, str) else "")
    if model is None:
        raise ValueError("unknown_type")
    try:
        return model.model_validate(obj)  # type: ignore[return-value]
    except ValidationError:
        raise ValueError("bad_frame") from None


# ---------------------------------------------------------------- server side
def message_frame(*, id: str, name: str, text: str, ts: float,
                  giles: bool = False) -> dict[str, Any]:
    return {"type": "message", "id": id, "name": name, "text": text,
            "ts": ts, "giles": giles}


def welcome_frame(*, name: str | None, banned_until: float | None,
                  slow_mode: float | None, last_id: str,
                  history: list[dict[str, Any]],
                  cooldown_until: float | None = None) -> dict[str, Any]:
    return {"type": "welcome", "name": name, "banned_until": banned_until,
            "cooldown_until": cooldown_until, "slow_mode": slow_mode,
            "last_id": last_id, "history": history}


def error_frame(code: str, detail: str = "") -> dict[str, Any]:
    return {"type": "error", "code": code, "detail": detail}


def ack_frame(client_msg_id: str, id: str) -> dict[str, Any]:
    return {"type": "ack", "client_msg_id": client_msg_id, "id": id}


def deleted_frame(id: str) -> dict[str, Any]:
    return {"type": "deleted", "id": id}


def renamed_frame(user_pseudonym: str, name: str) -> dict[str, Any]:
    return {"type": "renamed", "user_pseudonym": user_pseudonym, "name": name}


def warned_frame(reason: str) -> dict[str, Any]:
    return {"type": "warned", "reason": reason}


def banned_frame(until: float) -> dict[str, Any]:
    return {"type": "banned", "until": until}


def cooldown_frame(until: float) -> dict[str, Any]:
    return {"type": "cooldown", "until": until}


def slow_mode_frame(interval_s: float) -> dict[str, Any]:
    return {"type": "slow_mode", "interval_s": interval_s}


def pong_frame() -> dict[str, Any]:
    return {"type": "pong"}


def ping_frame() -> dict[str, Any]:
    return {"type": "ping"}


def dumps(frame: dict[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"))
