"""In-memory stand-in for the redis-py client slice `pilgrim.hotstate` uses
(AGENTS §1.2: tests never touch a real cache). TTLs expire against an
injected clock; `fail` injects a connection failure on the next call(s)."""
from __future__ import annotations

import asyncio
import builtins
from collections.abc import Callable
from typing import Any


class FakeConnectionError(Exception):
    pass


class FakeRedis:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self.fail_next = 0          # raise on this many upcoming calls
        self.calls: list[str] = []

    # -- plumbing -------------------------------------------------------
    def _check(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise FakeConnectionError(f"injected failure on {op}")

    def _sweep(self) -> None:
        now = self._now()
        for k in [k for k, (_, exp) in self._data.items() if exp is not None and exp <= now]:
            del self._data[k]

    def _put(self, name: str, value: Any, ex: int | None) -> None:
        raw = value if isinstance(value, bytes) else str(value).encode()
        self._data[name] = (raw, self._now() + ex if ex is not None else None)

    def ttl(self, name: str) -> int:
        self._sweep()
        if name not in self._data:
            return -2
        exp = self._data[name][1]
        return -1 if exp is None else int(round(exp - self._now()))

    # -- redis-py API slice --------------------------------------------
    def ping(self) -> bool:
        self._check("ping")
        return True

    def set(self, name: str, value: Any, ex: int | None = None) -> bool:
        self._check("set")
        self._put(name, value, ex)
        return True

    def get(self, name: str) -> bytes | None:
        self._check("get")
        self._sweep()
        v = self._data.get(name)
        return v[0] if v else None

    def getdel(self, name: str) -> bytes | None:
        self._check("getdel")
        self._sweep()
        v = self._data.pop(name, None)
        return v[0] if v else None

    def delete(self, *names: str) -> int:
        self._check("delete")
        if len(names) > 1:   # Valkey Serverless (cluster mode) refuses cross-slot multi-key ops
            raise FakeConnectionError("CROSSSLOT Keys in request don't hash to the same slot")
        return sum(1 for n in names if self._data.pop(n, None) is not None)

    def scan_iter(self, match: str | None = None, count: int | None = None):
        self._check("scan")
        self._sweep()
        prefix = (match or "*").rstrip("*")
        for k in list(self._data):
            if k.startswith(prefix):
                yield k.encode()

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        if transaction:      # MULTI across slots is a CROSSSLOT error on Serverless
            raise FakeConnectionError("CROSSSLOT transactional pipeline")
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r: FakeRedis) -> None:
        self._r = r
        self._ops: list[tuple[str, Any, int | None]] = []

    def set(self, name: str, value: Any, ex: int | None = None) -> _FakePipeline:
        self._ops.append((name, value, ex))
        return self

    def execute(self) -> list[bool]:
        self._r._check("pipeline")
        for name, value, ex in self._ops:
            self._r._put(name, value, ex)
        out = [True] * len(self._ops)
        self._ops = []
        return out


# --------------------------------------------------------------------------- #
# Async slice used by the Sheep Pen (SHEEP_PEN.md §0.1: FakeRedis has no
# streams/pub-sub/INCR/EXPIRE). Mirrors `redis.asyncio.Redis` enough for
# `pilgrim.chat.redis_store.ChatStore` and `ChatHub`; TTLs use the injected
# clock, Pub/Sub is one asyncio.Queue per subscriber per channel, `eval`
# implements the single-key INCR+EXPIRE script.
# --------------------------------------------------------------------------- #
class FakeAsyncRedis:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._data: dict[str, tuple[Any, float | None]] = {}
        self._sets: dict[str, builtins.set[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._seq = 0
        self.fail_next = 0
        self.calls: list[str] = []

    # -- plumbing -------------------------------------------------------
    async def _check(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise FakeConnectionError(f"injected failure on {op}")

    def _sweep(self) -> None:
        now = self._now()
        for k in [k for k, (_, exp) in self._data.items() if exp is not None and exp <= now]:
            del self._data[k]

    def _get(self, name: str) -> Any:
        self._sweep()
        v = self._data.get(name)
        return None if v is None else v[0]

    # -- redis-py API slice --------------------------------------------
    async def ping(self) -> bool:
        await self._check("ping")
        return True

    async def aclose(self) -> None:
        return None

    async def get(self, name: str) -> Any:
        await self._check("get")
        return self._get(name)

    async def set(self, name: str, value: Any, ex: int | None = None,
                  nx: bool = False) -> bool | None:
        await self._check("set")
        if nx and self._get(name) is not None:
            return None
        self._data[name] = (value, self._now() + ex if ex is not None else None)
        return True

    async def delete(self, *names: str) -> int:
        await self._check("delete")
        if len(names) > 1:
            raise FakeConnectionError("CROSSSLOT Keys in request don't hash to the same slot")
        n = 0
        for name in names:
            if self._data.pop(name, None) is not None:
                n += 1
            self._sets.pop(name, None)
            self._streams.pop(name, None)
        return n

    async def exists(self, name: str) -> int:
        await self._check("exists")
        return int(self._get(name) is not None or name in self._sets
                   or name in self._streams)

    async def incr(self, name: str) -> int:
        await self._check("incr")
        n = int(self._get(name) or 0) + 1
        exp = self._data.get(name, (None, None))[1]
        self._data[name] = (n, exp)
        return n

    async def expire(self, name: str, ttl: int) -> bool:
        await self._check("expire")
        if name not in self._data:
            return False
        self._data[name] = (self._data[name][0], self._now() + ttl)
        return True

    async def ttl(self, name: str) -> int:
        self._sweep()
        if name not in self._data:
            return -2
        exp = self._data[name][1]
        return -1 if exp is None else int(round(exp - self._now()))

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        """Only the chat rate-limit script exists: INCR + first-hit EXPIRE."""
        await self._check("eval")
        name, ttl = args[0], args[1]
        n = int(self._get(name) or 0) + 1
        exp = self._data.get(name, (None, None))[1]
        if n == 1:
            exp = self._now() + int(ttl)
        self._data[name] = (n, exp)
        return n

    # -- sets -----------------------------------------------------------
    async def sadd(self, name: str, *members: str) -> int:
        await self._check("sadd")
        s = self._sets.setdefault(name, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    async def smismember(self, name: str, members: list[str]) -> list[int]:
        await self._check("smismember")
        s = self._sets.get(name, set())
        return [int(m in s) for m in members]

    async def smembers(self, name: str) -> builtins.set[str]:
        await self._check("smembers")
        return set(self._sets.get(name, set()))

    async def scard(self, name: str) -> int:
        await self._check("scard")
        return len(self._sets.get(name, set()))

    async def srem(self, name: str, *members: str) -> int:
        await self._check("srem")
        s = self._sets.get(name, set())
        n = sum(1 for m in members if m in s)
        s.difference_update(members)
        return n

    # -- streams --------------------------------------------------------
    def _next_id(self) -> str:
        ms = int(self._now() * 1000)
        self._seq += 1
        return f"{ms}-{self._seq}"

    async def xadd(self, name: str, fields: dict[str, Any], maxlen: int | None = None,
                   approximate: bool = True) -> str:
        await self._check("xadd")
        entry_id = self._next_id()
        self._streams.setdefault(name, []).append((entry_id, dict(fields)))
        if maxlen:
            self._streams[name] = self._streams[name][-maxlen:]
        return entry_id

    def _in_range(self, entry_id: str, lo: str, hi: str) -> bool:
        key = tuple(int(x) for x in entry_id.split("-"))
        if lo != "-":
            excl = lo.startswith("(")
            lk = tuple(int(x) for x in (lo[1:] if excl else lo).split("-"))
            if key < lk or (excl and key == lk):
                return False
        if hi != "+":
            excl = hi.startswith("(")
            hk = tuple(int(x) for x in (hi[1:] if excl else hi).split("-"))
            if key > hk or (excl and key == hk):
                return False
        return True

    async def xrange(self, name: str, min: str = "-", max: str = "+",
                     count: int | None = None) -> list[tuple[str, dict[str, Any]]]:
        await self._check("xrange")
        rows = [(i, dict(f)) for i, f in self._streams.get(name, [])
                if self._in_range(i, min, max)]
        return rows[:count] if count else rows

    async def xrevrange(self, name: str, max: str = "+", min: str = "-",
                        count: int | None = None) -> list[tuple[str, dict[str, Any]]]:
        await self._check("xrevrange")
        rows = [(i, dict(f)) for i, f in reversed(self._streams.get(name, []))
                if self._in_range(i, min, max)]
        return rows[:count] if count else rows

    async def xlen(self, name: str) -> int:
        await self._check("xlen")
        return len(self._streams.get(name, []))

    async def xtrim(self, name: str, maxlen: int, approximate: bool = True) -> int:
        await self._check("xtrim")
        rows = self._streams.get(name, [])
        keep = rows[-maxlen:] if maxlen else []
        n = len(rows) - len(keep)
        self._streams[name] = keep
        return n

    async def xdel(self, name: str, *ids: str) -> int:
        await self._check("xdel")
        rows = self._streams.get(name, [])
        keep = [(i, f) for i, f in rows if i not in ids]
        self._streams[name] = keep
        return len(rows) - len(keep)

    # -- pub/sub --------------------------------------------------------
    async def publish(self, channel: str, message: Any) -> int:
        await self._check("publish")
        queues = self._subs.get(channel, [])
        for q in queues:
            q.put_nowait({"type": "message", "channel": channel, "data": message})
        return len(queues)

    def pubsub(self) -> FakeAsyncPubSub:
        return FakeAsyncPubSub(self)


class FakeAsyncPubSub:
    def __init__(self, r: FakeAsyncRedis) -> None:
        self._r = r
        self._queues: dict[str, asyncio.Queue] = {}

    async def subscribe(self, *channels: str) -> None:
        for ch in channels:
            q: asyncio.Queue = asyncio.Queue()
            self._queues[ch] = q
            self._r._subs.setdefault(ch, []).append(q)

    async def unsubscribe(self, *channels: str) -> None:
        names = channels or tuple(self._queues)
        for ch in names:
            q = self._queues.pop(ch, None)
            if q is not None and q in self._r._subs.get(ch, []):
                self._r._subs[ch].remove(q)

    async def get_message(self, timeout: float | None = None,
                          ignore_subscribe_messages: bool = True) -> Any:
        if not self._queues:
            return None
        q = next(iter(self._queues.values()))
        try:
            if timeout is None:
                return await q.get()
            return await asyncio.wait_for(q.get(), timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        await self.unsubscribe()

