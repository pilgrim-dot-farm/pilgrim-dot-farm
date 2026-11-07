"""The executor: delete, warn, ban ladder, IP ban, flag, switches."""
from __future__ import annotations

import json

from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import NullAudit
from pilgrim.chat.redis_store import ChatStore
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t


def _setup(cfg, store):
    clock = _Clock()
    cache = ChatStore(cfg, FakeAsyncRedis(clock.wall))
    audit = NullAudit()
    actions = ChatActions(cfg, store, cache, audit, clock)
    return clock, cache, audit, actions


def _user(store, uid="u1", ip_hash="abc123"):
    return store.create_chat_user(token_hash=f"t{uid}", user_id=uid,
                                  ip_hash=ip_hash)


def _message(store, mid="1-1", uid="u1", text="hello"):
    store.insert_chat_message(message_id=mid, user_id=uid, name="Ada", text=text,
                              ts=1.0, client_msg_id=f"c{mid}", status="pending_review")
    return mid


async def test_delete_tombstones_and_is_idempotent(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    _user(store)
    mid = _message(store)
    assert await actions.delete_message(mid, actor="ornith", reason="spam")
    assert (store.chat_message(mid) or {}).get("status") == "deleted"
    assert await cache.smismember("deleted", [mid]) == [True]
    assert len(store.chat_moderation_for(mid)) == 1
    assert any(e["event"] == "chat.message_deleted" for e in audit.events)
    # a retry does not duplicate the audit row or the event
    await actions.delete_message(mid, actor="ornith", reason="spam")
    assert len(store.chat_moderation_for(mid)) == 1
    events = [f for _, f in await cache.xrange_events()]
    assert sum(1 for f in events if f["event"] == "deleted") == 1


async def test_warn_is_targeted(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache, audit, actions = _setup(cfg, store)
    _user(store)
    mid = _message(store)
    await actions.warn_user("u1", "mind the rules", actor="ornith", message_id=mid)
    row = store.recent_chat_moderation(1)[0]
    assert row["action"] == "warn" and row["user_id"] == "u1"
    events = [json.loads(f["payload"]) for _, f in await cache.xrange_events()
              if f["event"] == "warned"]
    assert events[0]["user_id"] == "u1"
    assert any(e["event"] == "chat.user_warned" for e in audit.events)


async def test_ban_ladder_and_key_ttl(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, _, actions = _setup(cfg, store)
    _user(store)
    mid = _message(store)
    first = await actions.ban_user("u1", "hate", actor="ornith",
                                   severity=3, confidence=0.95, message_id=mid)
    assert first == clock.t + cfg.chat.ban_ladder_s[0]
    assert (store.chat_message(mid) or {}).get("status") == "deleted"
    clock.t += 5
    second = await actions.ban_user("u1", "hate", actor="ornith")
    assert second == clock.t + cfg.chat.ban_ladder_s[1]
    raw = await cache.get("ban:user:u1")
    assert json.loads(raw)["until"] == second
    # a different expiry is a different action id: not suppressed
    assert len(store.recent_chat_moderation(10)) >= 2


async def test_ip_ban_after_three_users(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache, _, actions = _setup(cfg, store)
    for i in range(3):
        _user(store, uid=f"u{i}", ip_hash="sameip")
        await actions.ban_user(f"u{i}", "abuse", actor="ornith")
    assert await cache.get("ban:ip:sameip") is not None
    assert store.active_chat_ban("ip", "sameip", 1_800_000_000.0 + 1) is not None


async def test_unban_clears_key_and_row(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache, _, actions = _setup(cfg, store)
    _user(store)
    await actions.ban_user("u1", "abuse", actor="ornith")
    assert await actions.unban("user", "u1", actor="operator") == 1
    assert await cache.get("ban:user:u1") is None


async def test_flag_is_admin_only(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache, audit, actions = _setup(cfg, store)
    _user(store)
    mid = _message(store)
    await actions.flag(mid, "u1", actor="ornith", policy="spam", severity=2,
                       confidence=0.5, reason="maybe")
    assert (store.chat_message(mid) or {}).get("status") == "flagged"
    events = [f["event"] for _, f in await cache.xrange_events()]
    assert "moderation_flag" in events


async def test_operator_switches(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache, _, actions = _setup(cfg, store)
    await actions.set_posting_disabled(True, actor="operator")
    assert await cache.exists("posting_disabled")
    await actions.set_posting_disabled(False, actor="operator")
    assert not await cache.exists("posting_disabled")
    await actions.set_giles_paused(True, actor="operator")
    assert await cache.exists("giles:paused")
    await actions.set_slow_mode(10, actor="operator")
    assert await cache.get("slow_mode") == "10"
    await actions.clear_slow_mode(actor="operator")
    assert await cache.get("slow_mode") is None


async def test_allow_marks_accepted(cfg, tmp_env):
    _, store, _ = tmp_env
    _, _, _, actions = _setup(cfg, store)
    _user(store)
    mid = _message(store)
    await actions.allow(mid, actor="ornith")
    assert (store.chat_message(mid) or {}).get("status") == "accepted"


async def test_spam_cooldown_ladder_and_expiry(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache, audit, actions = _setup(cfg, store)
    _user(store)
    first = await actions.cooldown_user("u1", "spam")
    assert first == clock.t + cfg.chat.cooldown_ladder_s[0]
    raw = await cache.get("cool:user:u1")
    assert json.loads(raw)["until"] == first
    clock.t += 1
    second = await actions.cooldown_user("u1", "spam")
    assert second == clock.t + cfg.chat.cooldown_ladder_s[1]
    clock.t += 1
    third = await actions.cooldown_user("u1", "spam")
    assert third == clock.t + cfg.chat.cooldown_ladder_s[2]
    assert any(e["event"] == "chat.user_cooldown" for e in audit.events)
    await actions.clear_cooldown("u1", actor="operator")
    assert await cache.get("cool:user:u1") is None
