"""Async Valkey slice: rate limits, streams, Pub/Sub, tombstones, reconnect."""
from __future__ import annotations

import asyncio

from pilgrim.chat.redis_store import ChatStore
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis, FakeConnectionError


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t


def _store(cfg):
    clock = _Clock()
    client = FakeAsyncRedis(clock.wall)
    return clock, client, ChatStore(cfg, client)


async def test_rate_hit_sets_ttl_atomically(cfg):
    clock, _, cache = _store(cfg)
    assert await cache.incr_with_ttl("rate:user:u1", 3) == 1
    assert await cache.incr_with_ttl("rate:user:u1", 3) == 2
    clock.t += 4
    assert await cache.incr_with_ttl("rate:user:u1", 3) == 1


async def test_streams_add_range_trim(cfg):
    _, _, cache = _store(cfg)
    ids = []
    for i in range(5):
        ids.append(await cache.xadd_message(
            {"user_id": "u", "name": "n", "text": f"m{i}", "ts": "1"}, 3))
    assert len(await cache.xrange_after("0-0")) == 3        # MAXLEN ~ kept 3
    recent = await cache.recent(2)
    assert [f["text"] for _, f in recent] == ["m4", "m3"]
    after = await cache.xrange_after(ids[3])
    assert [f["text"] for _, f in after] == ["m4"]
    assert await cache.xlen() == 3
    await cache.xtrim(1)
    assert await cache.xlen() == 1


async def test_pubsub_fans_out_to_every_task(cfg):
    _, _, cache = _store(cfg)
    ps1 = await cache.pubsub()
    ps2 = await cache.pubsub()
    assert await cache.publish({"type": "message", "id": "1-1"}) == 2
    assert (await ps1.get_message(timeout=1))["data"].find("1-1") >= 0
    assert (await ps2.get_message(timeout=1))["data"].find("1-1") >= 0
    await ps1.aclose()
    await ps2.aclose()


async def test_deleted_set_membership(cfg):
    _, _, cache = _store(cfg)
    await cache.sadd("deleted", "1-1", "2-2")
    assert await cache.smismember("deleted", ["1-1", "3-3"]) == [True, False]
    await cache.srem("deleted", "1-1")
    assert await cache.smismember("deleted", ["1-1"]) == [False]


async def test_failure_injection_is_a_connection_error(cfg):
    _, client, cache = _store(cfg)
    client.fail_next = 1
    try:
        await cache.ping()
    except FakeConnectionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected injected failure")
    assert await cache.ping() is True


async def test_backpressure_queue_is_bounded(cfg):
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait("one")
    try:
        q.put_nowait("two")
    except asyncio.QueueFull:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected QueueFull")
