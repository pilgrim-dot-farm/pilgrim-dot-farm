"""Async Valkey access for the Sheep Pen (SHEEP_PEN.md §0.5.3, §0.7).

One `redis.asyncio.Redis` client and one `pubsub()` per web task, built from
the same `REDIS_URL`/`cache.*` settings as `hotstate.connect`. `HotState` stays
sync and untouched.

Every chat key is `pilgrim:{chat}:<name>` — `cache.key_prefix` plus a literal
`{chat}` hash tag, so a Lua script or any multi-key command stays in one slot
(Valkey Serverless is cluster mode; CROSSSLOT otherwise). Only exact channel
names are used: serverless has no `PSUBSCRIBE` (SHEEP_PEN.md §0.4).
"""
from __future__ import annotations

import builtins
import contextlib
import json
import logging
from typing import Any

from pilgrim.config import Config

log = logging.getLogger("radio.chat.redis")

# Single-key INCR + first-hit EXPIRE, atomic. A crash can never leave an
# immortal rate-limit counter (SHEEP_PEN.md §9).
RATE_SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


class ChatStore:
    def __init__(self, cfg: Config, client: Any) -> None:
        self.cfg = cfg
        self.r = client
        self.prefix = f"{cfg.cache.key_prefix}{{chat}}:"

    def key(self, name: str) -> str:
        return self.prefix + name

    def channel(self) -> str:
        return self.key("pubsub")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.r.aclose()

    # ------------------------------------------------------------ plain keys
    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def get(self, name: str) -> Any:
        return await self.r.get(self.key(name))

    async def set(self, name: str, value: Any, ex: int | None = None) -> bool:
        return bool(await self.r.set(self.key(name), value, ex=ex))

    async def set_nx(self, name: str, value: Any, ex: int | None = None) -> bool:
        return bool(await self.r.set(self.key(name), value, ex=ex, nx=True))

    async def delete(self, name: str) -> int:
        return int(await self.r.delete(self.key(name)))

    async def exists(self, name: str) -> bool:
        return bool(await self.r.exists(self.key(name)))

    async def incr(self, name: str) -> int:
        return int(await self.r.incr(self.key(name)))

    async def expire(self, name: str, ttl: int) -> bool:
        return bool(await self.r.expire(self.key(name), ttl))

    async def get_json(self, name: str) -> Any:
        raw = await self.get(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    async def set_json(self, name: str, value: Any, ex: int | None = None) -> bool:
        return await self.set(name, json.dumps(value), ex=ex)

    # ------------------------------------------------------------ rate limits
    async def incr_with_ttl(self, name: str, ttl: int) -> int:
        """Atomic INCR + EXPIRE-on-first-hit for one key (one slot)."""
        return int(await self.r.eval(RATE_SCRIPT, 1, self.key(name), ttl))

    # ------------------------------------------------------------ sets
    async def sadd(self, name: str, *members: str) -> int:
        return int(await self.r.sadd(self.key(name), *members))

    async def smismember(self, name: str, members: list[str]) -> list[bool]:
        out = await self.r.smismember(self.key(name), members)
        return [bool(x) for x in out]

    async def smembers(self, name: str) -> builtins.set[str]:
        return set(await self.r.smembers(self.key(name)))

    async def scard(self, name: str) -> int:
        return int(await self.r.scard(self.key(name)))

    async def srem(self, name: str, *members: str) -> int:
        return int(await self.r.srem(self.key(name), *members))

    # ------------------------------------------------------------ streams
    async def xadd_message(self, fields: dict[str, Any],
                           maxlen: int) -> str:
        return str(await self.r.xadd(self.key("messages"), fields,
                                     maxlen=maxlen, approximate=True))

    async def xadd_event(self, fields: dict[str, Any],
                         maxlen: int = 5000) -> str:
        return str(await self.r.xadd(self.key("events"), fields,
                                     maxlen=maxlen, approximate=True))

    async def xrange_after(self, after_id: str, count: int | None = None) -> list[Any]:
        kwargs: dict[str, Any] = {"min": f"({after_id}", "max": "+"}
        if count:
            kwargs["count"] = count
        return list(await self.r.xrange(self.key("messages"), **kwargs))

    async def xrange_from(self, start_id: str, count: int | None = None) -> list[Any]:
        kwargs: dict[str, Any] = {"min": start_id, "max": "+"}
        if count:
            kwargs["count"] = count
        return list(await self.r.xrange(self.key("messages"), **kwargs))

    async def recent(self, count: int) -> list[Any]:
        return list(await self.r.xrevrange(self.key("messages"), max="+", min="-",
                                           count=count))

    async def xlen(self) -> int:
        return int(await self.r.xlen(self.key("messages")))

    async def xrange_events(self, count: int | None = None) -> list[Any]:
        kwargs: dict[str, Any] = {"min": "-", "max": "+"}
        if count:
            kwargs["count"] = count
        return list(await self.r.xrange(self.key("events"), **kwargs))

    async def xtrim(self, maxlen: int) -> int:
        return int(await self.r.xtrim(self.key("messages"), maxlen=maxlen,
                                      approximate=True))

    async def xdel(self, entry_id: str) -> int:
        return int(await self.r.xdel(self.key("messages"), entry_id))

    # ------------------------------------------------------------ pub/sub
    async def publish(self, frame: dict[str, Any]) -> int:
        return int(await self.r.publish(self.channel(),
                                        json.dumps(frame, separators=(",", ":"))))

    async def pubsub(self) -> Any:
        ps = self.r.pubsub()
        await ps.subscribe(self.channel())
        return ps


def connect(url: str, cfg: Config) -> ChatStore:
    """Async client for the chat path. Connects lazily."""
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(
        url, socket_timeout=cfg.cache.timeout_s,
        socket_connect_timeout=cfg.cache.timeout_s,
        health_check_interval=30, decode_responses=True)
    return ChatStore(cfg, client)
