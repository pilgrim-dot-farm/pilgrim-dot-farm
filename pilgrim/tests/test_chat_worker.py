"""The Mac worker loop: ticks review, Giles, purge, heartbeat, then stops."""
from __future__ import annotations

import asyncio

from pilgrim import chat_worker


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t


class _Cache:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, name, value, ex=None):
        self.keys[name] = value
        return True

    async def close(self):
        pass


class _Reviewer:
    def __init__(self) -> None:
        self.reviews = 0
        self.purges = 0

    async def run_once(self):
        self.reviews += 1
        return {"reviewed": 0}

    async def purge(self):
        self.purges += 1
        return {}


class _Giles:
    def __init__(self) -> None:
        self.dispatches = 0

    async def maybe_dispatch(self):
        self.dispatches += 1
        return None


async def test_run_ticks_everything_and_stops(monkeypatch, cfg):
    monkeypatch.setattr(chat_worker, "TICK_S", 0.01)
    cache, reviewer, giles = _Cache(), _Reviewer(), _Giles()
    worker = {"cfg": cfg, "cache": cache, "reviewer": reviewer,
              "giles": giles, "clock": _Clock()}
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(_stop_soon())
    await asyncio.wait_for(chat_worker.run(worker, stop), timeout=2)
    await stopper
    assert reviewer.reviews >= 1
    assert reviewer.purges >= 1
    assert giles.dispatches >= 1
    assert "worker:heartbeat" in cache.keys


async def test_run_survives_a_failing_component(monkeypatch, cfg):
    monkeypatch.setattr(chat_worker, "TICK_S", 0.01)
    cache = _Cache()

    class _Bad:
        async def run_once(self):
            raise RuntimeError("down")

        async def purge(self):
            raise RuntimeError("down")

        async def maybe_dispatch(self):
            raise RuntimeError("down")

    worker = {"cfg": cfg, "cache": cache, "reviewer": _Bad(), "giles": _Bad(),
              "clock": _Clock()}
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.03)
        stop.set()

    stopper = asyncio.create_task(_stop_soon())
    await asyncio.wait_for(chat_worker.run(worker, stop), timeout=2)
    await stopper
    assert "worker:heartbeat" in cache.keys
