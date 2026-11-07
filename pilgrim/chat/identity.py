"""Chat identity: browser token, server-side user, unique display names.

SHEEP_PEN.md §4, §0.6.1. No accounts, passwords or email. The browser's
existing 32-hex `pd_voter` id is the token; the server hashes it and maps the
hash to a server-generated user id. Postgres is the name-uniqueness authority
(a partial unique index); the Valkey reservation is only the cheap pre-check.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import unicodedata
from datetime import UTC, datetime

from pilgrim.chat import prohibited
from pilgrim.chat.redis_store import ChatStore
from pilgrim.config import Chat, Config
from pilgrim.pipelines.request_filter import prefilter
from pilgrim.store import Store

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


class NameError_(Exception):
    """A display-name rejection carrying a stable, nonrevealing code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def valid_token(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ip_hash(canonical_ip: str | None) -> str | None:
    """SHEEP_PEN.md §0.6.5: plain SHA-256 prefix, no secret, no raw IP."""
    if not canonical_ip:
        return None
    return hashlib.sha256(canonical_ip.encode("utf-8")).hexdigest()[:16]


def normalize_name(display: str) -> str:
    """NFKC → casefold → collapse whitespace → strip (uniqueness key)."""
    folded = unicodedata.normalize("NFKC", display).casefold()
    return " ".join(folded.split())


def now_iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else datetime.now(UTC).timestamp(),
                                  tz=UTC).isoformat()


def validate_name(display: str, cfg: Chat) -> str | None:
    """Return a stable error code, or None when the display name is usable.

    Unicode-aware: length counts code points, control/format/invisible
    characters are refused, HTML angle brackets are refused, links are refused
    by the station's existing prefilter, prohibited terms and reserved names by
    the digest lists. No LLM is involved (SHEEP_PEN.md §4).
    """
    name = display.strip()
    if len(name) < cfg.name_min or len(name) > cfg.name_max:
        return "name_length"
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return "name_chars"
    if "<" in name or ">" in name:
        return "name_chars"
    if prefilter(name):
        return "name_link"
    if prohibited.prohibited_hit(name):
        return "name_invalid"
    if prohibited.is_reserved_name(name):
        return "name_reserved"
    return None


async def resolve_user(store: Store, cache: ChatStore, token: str,
                       canonical_ip: str | None) -> dict:
    """Resolve (or create) the durable user behind a browser token.

    The Valkey session key is a cache; Postgres `chat_users` is the truth. A
    cache failure falls through to Postgres instead of failing the socket.
    """
    th = token_hash(token)
    uid: str | None = None
    try:
        uid = await cache.get_json(f"session:{th}")
    except Exception:  # noqa: BLE001 - a cache miss is not an identity failure
        uid = None
    user = store.chat_user(uid) if isinstance(uid, str) else None
    if user is None:
        user = store.chat_user_by_token(th)
    if user is None:
        user = store.create_chat_user(token_hash=th, ip_hash=ip_hash(canonical_ip))
    with contextlib.suppress(Exception):
        await cache.set_json(f"session:{th}", user["user_id"], ex=60 * 86400)
    store.touch_chat_user(user["user_id"])
    return user


async def claim_name(store: Store, cache: ChatStore, cfg: Config, user: dict,
                     display: str, *, epoch: float | None = None,
                     allow_change: bool = False) -> dict:
    """Claim a display name. Raises `NameError_` with a code.

    Codes: the validation codes above, `name_taken` (409), `name_locked`
    (409: a claimed name belongs to that user; operators can still rename via
    `allow_change=True`). Re-claiming the same normalized name is idempotent.
    A permitted change keeps `user_id`, so existing messages re-render with the
    new name without rewriting the stream.
    """
    code = validate_name(display, cfg.chat)
    if code:
        raise NameError_(code)
    name = display.strip()
    norm = normalize_name(name)
    uid = user["user_id"]

    if user.get("name_norm"):
        if norm == user["name_norm"]:
            return user
        if not allow_change and not cfg.chat.allow_name_changes:
            raise NameError_("name_locked")

    # Cheap pre-check, never the authority. A failure to reach the cache is
    # not a rejection: Postgres still decides.
    reservation = f"name:{norm}"
    try:
        got = await cache.set_nx(reservation, uid,
                           ex=cfg.chat.name_release_days * 86400)
        if not got:
            holder = await cache.get(reservation)
            if holder not in (uid, None):
                raise NameError_("name_taken")
    except NameError_:
        raise
    except Exception:  # noqa: BLE001
        pass

    ok, updated = store.claim_chat_name(uid, name, norm, now=now_iso(epoch))
    if not ok:
        # Do not poison the reservation key with a loser's id.
        with contextlib.suppress(Exception):
            if await cache.get(reservation) == uid:
                await cache.delete(reservation)
        raise NameError_("name_taken")

    old_norm = user.get("name_norm")
    if old_norm and old_norm != norm:
        with contextlib.suppress(Exception):
            await cache.delete(f"name:{old_norm}")
    return updated or {**user, "name": name, "name_norm": norm}


async def release_name(store: Store, cache: ChatStore, user_id: str,
                       old_norm: str | None, *, epoch: float | None = None) -> None:
    store.release_chat_name(user_id, now=now_iso(epoch))
    if old_norm:
        with contextlib.suppress(Exception):
            await cache.delete(f"name:{old_norm}")
