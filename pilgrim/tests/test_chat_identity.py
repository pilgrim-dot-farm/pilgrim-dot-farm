"""Chat identity: tokens, names, uniqueness, reservation, release."""
from __future__ import annotations

import pytest
from pilgrim.chat import identity
from pilgrim.chat.redis_store import ChatStore
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis

TOKEN_A = "0123456789abcdef0123456789abcdef"
TOKEN_B = "fedcba9876543210fedcba9876543210"


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def wall(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _pair(cfg):
    clock = _Clock()
    cache = ChatStore(cfg, FakeAsyncRedis(clock.wall))
    return clock, cache


def test_token_and_ip_hash():
    assert identity.valid_token(TOKEN_A)
    assert not identity.valid_token("short")
    assert not identity.valid_token("Z" * 32)
    assert len(identity.token_hash(TOKEN_A)) == 64
    h = identity.ip_hash("203.0.113.9")
    assert h is not None and len(h) == 16
    assert identity.ip_hash(None) is None


def test_normalize_name_nfkc_casefold_whitespace():
    assert identity.normalize_name("  GiLes\u00a0  ") == "giles"
    assert identity.normalize_name("ＡＤＡＭ") == "adam"
    assert identity.normalize_name("Rosie   Thistlebottom") == "rosie thistlebottom"


def test_validate_name(cfg):
    c = cfg.chat
    assert identity.validate_name("Ada", c) is None
    assert identity.validate_name("ab", c) == "name_length"
    assert identity.validate_name("x" * 25, c) == "name_length"
    assert identity.validate_name("bad\u200bname", c) == "name_chars"
    assert identity.validate_name("<b>bo</b>", c) == "name_chars"
    assert identity.validate_name("see example.com", c) == "name_link"
    assert identity.validate_name("Giles", c) == "name_reserved"
    assert identity.validate_name("operator", c) == "name_reserved"


async def test_two_users_race_for_one_name(cfg, tmp_env):
    _, store, _ = tmp_env
    clock, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, "203.0.113.1")
    b = await identity.resolve_user(store, cache, TOKEN_B, "203.0.113.2")
    first = await identity.claim_name(store, cache, cfg, a, "Thistle")
    assert first["name"] == "Thistle"
    with pytest.raises(identity.NameError_) as e:
        await identity.claim_name(store, cache, cfg, b, "thistle")
    assert e.value.code == "name_taken"
    # the loser keeps no name and cannot post
    assert (store.chat_user(b["user_id"]) or {}).get("name") is None


async def test_nfkc_variants_collide(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, None)
    b = await identity.resolve_user(store, cache, TOKEN_B, None)
    await identity.claim_name(store, cache, cfg, a, "Adam")
    with pytest.raises(identity.NameError_) as e:
        await identity.claim_name(store, cache, cfg, b, "ＡＤＡＭ")
    assert e.value.code == "name_taken"


async def test_released_name_can_be_reclaimed(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, None)
    b = await identity.resolve_user(store, cache, TOKEN_B, None)
    await identity.claim_name(store, cache, cfg, a, "Barn")
    await identity.release_name(store, cache, a["user_id"], "barn")
    got = await identity.claim_name(store, cache, cfg, b, "barn")
    assert got["name"] == "barn"


async def test_a_claimed_name_is_locked(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, None)
    await identity.claim_name(store, cache, cfg, a, "First")
    user = store.chat_user(a["user_id"]) or {}
    # changing to a free name is refused too (operator decision 2026-11-07)
    with pytest.raises(identity.NameError_) as e:
        await identity.claim_name(store, cache, cfg, user, "Second")
    assert e.value.code == "name_locked"
    assert (store.chat_user(a["user_id"]) or {}).get("name") == "First"


async def test_reclaiming_the_same_name_is_idempotent(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, None)
    first = await identity.claim_name(store, cache, cfg, a, "SameName")
    user = store.chat_user(a["user_id"]) or {}
    again = await identity.claim_name(store, cache, cfg, user, "samename")
    assert again["name"] == first["name"] == "SameName"


async def test_operator_rename_can_change_a_locked_name(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    a = await identity.resolve_user(store, cache, TOKEN_A, None)
    await identity.claim_name(store, cache, cfg, a, "First")
    user = store.chat_user(a["user_id"]) or {}
    updated = await identity.claim_name(store, cache, cfg, user, "Second",
                                        allow_change=True)
    assert updated["name"] == "Second"


async def test_session_round_trip(cfg, tmp_env):
    _, store, _ = tmp_env
    _, cache = _pair(cfg)
    first = await identity.resolve_user(store, cache, TOKEN_A, "203.0.113.5")
    again = await identity.resolve_user(store, cache, TOKEN_A, "203.0.113.5")
    assert first["user_id"] == again["user_id"]
    assert (store.chat_user(first["user_id"]) or {}).get("ip_hash") == \
        identity.ip_hash("203.0.113.5")
