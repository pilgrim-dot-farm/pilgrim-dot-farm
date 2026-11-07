"""Store: chat tables, name uniqueness index, pending review, purge."""
from __future__ import annotations


def _user(store, uid, token, name=None, norm=None):
    store.create_chat_user(token_hash=token, user_id=uid, ip_hash="h" * 16)
    if name:
        store.claim_chat_name(uid, name, norm or name.casefold())


def test_name_uniqueness_is_the_store_authority(tmp_env):
    _, store, _ = tmp_env
    _user(store, "u1", "t1", "Ada", "ada")
    _user(store, "u2", "t2", "Bram", "bram")
    ok, _ = store.claim_chat_name("u2", "Ada", "ada")
    assert not ok
    assert (store.chat_user("u1") or {})["name"] == "Ada"
    assert (store.chat_user("u2") or {})["name"] == "Bram"


def test_released_name_frees_the_index(tmp_env):
    _, store, _ = tmp_env
    _user(store, "u1", "t1", "Ada", "ada")
    store.release_chat_name("u1")
    ok, _ = store.claim_chat_name("u2", "Ada", "ada")
    assert ok


def test_chat_messages_pending_and_purge(tmp_env):
    _, store, _ = tmp_env
    assert store.insert_chat_message(message_id="1-1", user_id="u1", name="Ada",
                                     text="secret", ts=10.0,
                                     client_msg_id="c1", status="pending_review")
    assert not store.insert_chat_message(message_id="1-1", user_id="u1",
                                         name="Ada", text="dup", ts=10.0,
                                         client_msg_id="c2")
    pending = store.pending_chat_messages()
    assert [r["id"] for r in pending] == ["1-1"]
    store.mark_chat_message_deleted("1-1", "ornith", now="2026-01-01T00:00:00+00:00")
    row = store.chat_message("1-1") or {}
    assert row["status"] == "deleted" and row["deleted_by"] == "ornith"
    n_msg, n_mod = store.purge_chat_text_before(100.0, "2026-01-01T00:00:00+00:00")
    assert n_msg == 1
    assert (store.chat_message("1-1") or {}).get("text") is None
    assert (store.chat_message("1-1") or {}).get("status") == "deleted"


def test_ban_rows_and_moderation_idempotency(tmp_env):
    _, store, _ = tmp_env
    store.insert_chat_ban(scope="user", target="u1", until=999.0,
                          reason="x", created_at="2026-01-01T00:00:00+00:00")
    assert store.active_chat_ban("user", "u1", 100.0)
    assert not store.active_chat_ban("user", "u1", 1000.0)
    assert store.insert_chat_moderation(action_id="a1", message_id="1-1",
                                        user_id="u1", action="delete",
                                        policy="hate", severity=3,
                                        confidence=0.9, reason="r", text="t",
                                        decided_by="ornith")
    assert not store.insert_chat_moderation(action_id="a1", message_id="1-1",
                                            user_id="u1", action="delete",
                                            policy="hate", severity=3,
                                            confidence=0.9, reason="r", text="t",
                                            decided_by="ornith")
    assert store.bans_since("user", "u1", "2025-12-01T00:00:00+00:00") == 1
