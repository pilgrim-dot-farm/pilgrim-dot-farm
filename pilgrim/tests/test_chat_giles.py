"""Giles: exactly-once canon sign-off, idle vs active, cooldown, pause."""
from __future__ import annotations

from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import NullAudit
from pilgrim.chat.giles import Giles, enforce_signoff, signoff
from pilgrim.chat.redis_store import ChatStore
from pilgrim.tests.fakes import FakeLLM
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis

NAME = "Giles"


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _setup(cfg, store):
    clock = _Clock()
    cache = ChatStore(cfg, FakeAsyncRedis(clock.wall))
    audit = NullAudit()
    actions = ChatActions(cfg, store, cache, audit, clock)
    return clock, cache, audit, actions


def _giles(cfg, store, cache, audit, llm, clock):
    return Giles(cfg, store, cache, llm, {"field_chat": "Dispatch as {name}."},
                 clock=clock, audit=audit)  # type: ignore[arg-type]


def test_signoff_none_once_twice():
    canon = signoff(NAME)
    assert canon.lower().count("outstanding in my field") == 1

    none = "The goats have formed a committee."
    out = enforce_signoff(none, NAME)
    assert out.endswith(canon)
    assert out.lower().count("outstanding in my field") == 1

    once = f"Giles here. {canon}"
    out = enforce_signoff(once, NAME)
    assert out.endswith(canon)
    assert out.lower().count("outstanding in my field") == 1

    twice = (f"The tractor is outstanding in my field. {canon} "
             f"And I am outstanding in my field too.")
    out = enforce_signoff(twice, NAME)
    assert out.endswith(canon)
    assert out.lower().count("outstanding in my field") == 1


async def test_idle_dispatch_is_fictional_and_canon(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    llm = FakeLLM(responses={"Write one dispatch": {
        "text": "The chickens have formed a zoning committee. "
                "Outstanding in my field, they say. "
                "This is Giles signing off with a reminder that I'm outstanding in my field."}})
    g = _giles(cfg, store, cache, audit, llm, clock)
    out = await g.maybe_dispatch()
    assert out and out["mode"] == "idle"
    assert out["text"].endswith(signoff(NAME))
    assert out["text"].lower().count("outstanding in my field") == 1
    assert len(out["text"]) <= cfg.chat.giles_max_chars


async def test_active_dispatch_after_enough_messages(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    for i in range(cfg.chat.giles_min_messages):
        await cache.xadd_message({"user_id": f"u{i}", "name": "Ada",
                                  "text": f"message {i}", "ts": str(clock.t)},
                                 cfg.chat.history_max)
    llm = FakeLLM(responses={"Write one dispatch": {
        "text": "The pen is debating tractors. This is Giles signing off with a "
                "reminder that I'm outstanding in my field."}})
    g = _giles(cfg, store, cache, audit, llm, clock)
    out = await g.maybe_dispatch()
    assert out and out["mode"] == "active"
    assert out["text"].lower().count("outstanding in my field") == 1


async def test_cooldown_and_pause(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    llm = FakeLLM(responses={"Write one dispatch": {
        "text": "Tractor news. This is Giles signing off with a reminder that "
                "I'm outstanding in my field."}})
    g = _giles(cfg, store, cache, audit, llm, clock)
    assert await g.maybe_dispatch() is not None
    assert await g.maybe_dispatch() is None          # cooldown
    clock.advance(max(cfg.chat.giles_cooldown_s,
                      cfg.chat.giles_idle_interval_s) + 1)
    await cache.set("giles:paused", "1")
    assert await g.maybe_dispatch() is None          # paused
    await cache.delete("giles:paused")
    assert await g.maybe_dispatch() is not None


async def test_deleted_messages_never_feed_giles(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    mid = await cache.xadd_message({"user_id": "u1", "name": "Ada",
                                    "text": "secret", "ts": str(clock.t)},
                                   cfg.chat.history_max)
    await cache.sadd("deleted", mid)
    g = _giles(cfg, store, cache, audit, FakeLLM(), clock)
    recent = await g._recent()
    assert all(m["id"] != mid for m in recent)


async def test_clamp_keeps_signoff(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    long_text = ("A very long barnyard report. " * 30 +
                 "This is Giles signing off with a reminder that I'm outstanding in my field.")
    llm = FakeLLM(responses={"Write one dispatch": {"text": long_text}})
    g = _giles(cfg, store, cache, audit, llm, clock)
    out = await g.maybe_dispatch()
    assert out is not None
    assert len(out["text"]) <= cfg.chat.giles_max_chars
    assert out["text"].endswith(signoff(NAME))
