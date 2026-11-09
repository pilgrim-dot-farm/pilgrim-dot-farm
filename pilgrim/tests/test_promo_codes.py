"""Promo codes for the paid request line (2026-11-08): N uses per code, one
redemption per identity, exhausted codes retired but never deleted."""
from __future__ import annotations

from pilgrim.store import Store


def test_create_and_list(tmp_env):
    _, store, _ = tmp_env
    row = store.create_promo_code("ride-free", 3, "launch")
    assert row["code"] == "RIDE-FREE"
    assert row["max_uses"] == 3 and row["uses"] == 0
    assert row["active"] == 1
    assert store.list_promo_codes()[0]["code"] == "RIDE-FREE"
    # case-insensitive lookup
    assert store.promo_code("Ride-Free")["id"] == row["id"]


def test_one_redemption_per_identity(tmp_env):
    _, store, _ = tmp_env
    store.create_promo_code("ONE-EACH", 5)
    first = store.redeem_promo_code("ONE-EACH", "voter:a" * 4)
    assert first["ok"] and first["uses"] == 1
    again = store.redeem_promo_code("ONE-EACH", "voter:a" * 4)
    assert again["ok"] is False and again["code"] == "used_by_identity"
    other = store.redeem_promo_code("ONE-EACH", "voter:b" * 4)
    assert other["ok"] and other["uses"] == 2


def test_exhausted_code_is_retired_not_deleted(tmp_env):
    _, store, _ = tmp_env
    store.create_promo_code("ONCE", 1)
    assert store.redeem_promo_code("ONCE", "voter:1")["ok"] is True
    row = store.promo_code("ONCE")
    assert row["uses"] == 1 and row["active"] == 0
    assert row["retired_by"] == "exhausted" and row["retired_at"]
    # it is still listed, and a guess says "all used up", not "invalid"
    assert any(r["code"] == "ONCE" for r in store.list_promo_codes())
    status = store.promo_code_status("ONCE", "voter:2")
    assert status["ok"] is False and status["code"] == "exhausted"
    assert store.redeem_promo_code("ONCE", "voter:2")["code"] == "exhausted"


def test_invalid_code(tmp_env):
    _, store, _ = tmp_env
    status = store.promo_code_status("NOPE", "voter:1")
    assert status["ok"] is False and status["code"] == "invalid"
    assert store.redeem_promo_code("NOPE", "voter:1")["code"] == "invalid"


def test_release_returns_a_use_and_reactivates_exhausted(tmp_env):
    _, store, _ = tmp_env
    store.create_promo_code("RETRY", 1)
    assert store.redeem_promo_code("RETRY", "voter:1")["ok"] is True
    assert store.promo_code("RETRY")["active"] == 0
    assert store.release_promo_redemption("RETRY", "voter:1") is True
    row = store.promo_code("RETRY")
    assert row["uses"] == 0 and row["active"] == 1
    assert row["retired_at"] is None and row["retired_by"] is None
    # the identity may try again
    assert store.redeem_promo_code("RETRY", "voter:1")["ok"] is True


def test_operator_retired_code_stays_retired(tmp_env):
    _, store, _ = tmp_env
    store.create_promo_code("OFF", 2)
    assert store.redeem_promo_code("OFF", "voter:1")["ok"] is True
    assert store.retire_promo_code("OFF") is True
    assert store.release_promo_redemption("OFF", "voter:1") is True
    row = store.promo_code("OFF")
    assert row["active"] == 0 and row["retired_by"] == "operator"
    assert store.redeem_promo_code("OFF", "voter:2")["code"] == "exhausted"
    assert store.activate_promo_code("OFF") is True
    assert store.promo_code("OFF")["active"] == 1


def test_redemptions_are_recorded(tmp_env):
    _, store, _ = tmp_env
    store.create_promo_code("LOG", 2)
    store.redeem_promo_code("LOG", "voter:1", request_id=42)
    rows = store.promo_redemptions("LOG")
    assert len(rows) == 1
    assert rows[0]["identity"] == "voter:1" and rows[0]["request_id"] == 42


def test_store_rejects_bad_input(tmp_env):
    _, store, _ = tmp_env
    try:
        store.create_promo_code("", 1)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("empty code accepted")
    try:
        store.create_promo_code("X", 0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("zero uses accepted")


def test_sqlite_and_postgres_schema_agree(tmp_env):
    """The tables exist on whichever engine the suite is running (make pg-test)."""
    _, store, _ = tmp_env
    assert isinstance(store, Store)
    assert "promo_codes" in store._conn.tables()
    assert "promo_redemptions" in store._conn.tables()
