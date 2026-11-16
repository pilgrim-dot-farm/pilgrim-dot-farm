"""Song-job queueing: priority and slot gates, the job record, tuning constants (RADIO §6.1)."""
from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("radio.producer")


# cut-off song briefs re-queued without spending a request attempt, per
# request; after that they count like any failure (a request that always cuts
# off must still end, and a paid one get its refund flag)
LLM_FREE_CUTS = 4


class PriorityGate:
    """Up to `capacity` holders at a time, each grant going to the waiter with
    the LOWEST key, not the earliest (asyncio.Lock is FIFO: a paid request
    re-queued after a cut-off brief waited behind a free one, 2026-11-01).
    `capacity` 1 is the old one-holder gate; the song-brief gate takes the
    brief model's `models.max_concurrency` (RADIO §6.1/§12) so several briefs
    may be written at once (2026-11-14)."""

    def __init__(self, capacity: int = 1) -> None:
        self._capacity = max(1, int(capacity))
        self._busy = 0
        self._waiters: list[tuple[tuple, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def _release(self) -> None:
        self._busy -= 1
        while self._busy < self._capacity and self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                self._busy += 1  # hand off: the slot now belongs to the new holder
                fut.set_result(None)
                return

    @contextlib.asynccontextmanager
    async def hold(self, key: tuple) -> AsyncIterator[None]:
        if self._busy < self._capacity:
            self._busy += 1
        else:
            fut = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiters, (key, next(self._seq), fut))
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()  # granted as we were cancelled: pass it on
                raise
        try:
            yield
        finally:
            self._release()


# stock briefs queue behind every request brief (keys sort by first element)
STOCK_BRIEF_KEY = (1,)
# stock renders queue behind every request render (same ordering)
STOCK_RENDER_KEY = (1,)
# One request brief may be written ahead of the render slots: the next
# request's words are written while the songs ahead of it render (RADIO §6.1).
# Paid requests are bounded at C + 1 jobs; free requests at one job past the
# reserved-slot rule and songs.free_request_slots, and that extra one waits at
# the render gate (SlotGate free_caps) until a free-work slot opens.
BRIEFS_AHEAD = 1
# How often the producer re-reads whole tables for housekeeping that rarely
# has work (pool turnover, a finished intro backfill).
SLOW_CHECK_S = 600.0


def _created_epoch(item: dict) -> float:
    try:
        return datetime.fromisoformat(str(item.get("created_at"))).timestamp()
    except ValueError:
        return 0.0


def _bench_opts(req: dict) -> dict[str, Any] | None:
    """A benchmark row's options, or None for a listener's request
    (docs/systems/queue-bench.md)."""
    if not req.get("bench"):
        return None
    try:
        opts = json.loads(req.get("bench_opts") or "{}")
    except ValueError:
        opts = {}
    return opts if isinstance(opts, dict) else {}


@contextlib.asynccontextmanager
async def _no_gate() -> AsyncIterator[None]:
    """Stands in for the brief gate when no brief is written."""
    yield


class SlotGate:
    """`capacity()` holders at a time, granted to the waiter with the LOWEST
    key, not the earliest (RADIO §6.1: a paid request must not wait for a
    render behind free work that already has its brief).

    The render gate. Capacity is re-read on every grant, so a backend back
    from cooldown lets the queue through on the next `pump()`. Grants happen
    on release and on `pump()` only: the song loop is the only clock here
    (hard rule 3), never a timer of its own.

    `hold_free()` is asked before granting a free/stock waiter: while a paid
    request is still writing its brief, free work stays in line so the paid
    song renders first (RADIO §6.1).

    `free_caps()` returns `(free_total, free_requests)`: how many free holders
    (requests + stock) and how many free request holders may render at once.
    A free request whose brief was written ahead of its slot waits here
    until one of those opens, so the slot reserved for a paid request stays
    open (RADIO §6.1). A waiter the caps hold back does not block the ones
    behind it."""

    def __init__(self, capacity: Callable[[], int],
                 hold_free: Callable[[], bool] | None = None,
                 free_caps: Callable[[], tuple[int, int]] | None = None) -> None:
        self._capacity = capacity
        self._hold_free = hold_free
        self._free_caps = free_caps
        self._active = 0
        self._free_active = 0       # free requests + stock holding a slot
        self._free_req_active = 0   # free requests holding a slot
        self._waiters: list[tuple[tuple, int, bool, bool, asyncio.Future]] = []
        self._seq = itertools.count()

    def _free_fits(self, stock: bool) -> bool:
        if self._free_caps is None:
            return True
        total, requests = self._free_caps()
        if self._free_active >= total:
            return False
        return stock or self._free_req_active < requests

    def pump(self) -> None:
        cap = max(0, int(self._capacity()))
        self._waiters = [w for w in self._waiters if not w[4].done()]
        for w in sorted(self._waiters, key=lambda w: w[:2]):
            if self._active >= cap:
                return
            _, _, free, stock, fut = w
            if free:
                if self._hold_free is not None and self._hold_free():
                    continue  # a paid request is still writing: free work stays in line
                if not self._free_fits(stock):
                    continue  # written ahead of its slot: waits for a free-work slot
                self._free_active += 1
                self._free_req_active += 0 if stock else 1
            self._waiters.remove(w)
            self._active += 1
            fut.set_result(None)  # granted: counts until the holder releases

    def _release(self, free: bool, stock: bool) -> None:
        self._active -= 1
        if free:
            self._free_active -= 1
            self._free_req_active -= 0 if stock else 1
        self.pump()

    async def _acquire(self, key: tuple, free: bool, stock: bool) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((key, next(self._seq), free, stock, fut))
        try:
            self.pump()
            if not fut.done():
                await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release(free, stock)   # granted as we were cancelled: pass it on
            fut.cancel()
            raise

    @contextlib.asynccontextmanager
    async def hold(self, key: tuple, free: bool = False,
                   stock: bool = False) -> AsyncIterator[None]:
        await self._acquire(key, free, stock)
        try:
            yield
        finally:
            self._release(free, stock)


@dataclass
class SongJob:
    """One unit of song work: brief -> render -> intro -> store (RADIO §6.1)."""
    kind: str                      # "request" | "stock"
    req: dict | None = None
    genre: str | None = None       # set once the brief exists (genre context)
    # "brief" while the words are being written, "render" from the moment it
    # waits for a generator slot: only "render" competes for the pool (§6.1).
    # "done" once the song model has returned: what is left (intro, waiting
    # for the requests ahead of it) holds no slot, so it no longer counts
    # against the jobs the decider may start
    phase: str = "brief"
    # set when the job ends (ready, failed or re-queued): releases later
    # requests held behind it
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def paid(self) -> bool:
        return bool(self.req and self.req.get("priority"))

    def queue_key(self) -> tuple:
        """Position in queue order (store.queue_order: paid first, in flight
        by claim time, then free by vote score, then FIFO)."""
        r = self.req or {}
        return (-int(r.get("priority") or 0),
                str(r.get("paid_at") or r.get("claimed_at")
                    or r.get("created_at") or ""),
                int(r.get("id") or 0))

# Copy brief for the emergency station IDs (RADIO §8.3): timeless on purpose,
# since they may air any hour, any day.
STATION_ID_BRIEF = (
    "Write a plain station ID, one or two short sentences: say the station name, "
    "Pilgrim Dot Farm, and that the listener is tuned in. Timeless: no time of "
    "day, no weather, no song names, no news.")
