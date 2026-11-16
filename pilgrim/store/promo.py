"""Promo codes (RADIO §14).
"""
from __future__ import annotations

from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    _now_iso,
    _promo_norm,
)


class PromoStore(StoreBase):
    # ---------------------------------------------------------- promo codes
    # Paid-request promo codes (2026-11-08): N uses per code, one redemption
    # per identity, exhausted codes retired but never deleted.
    def create_promo_code(self, code: str, max_uses: int,
                          note: str | None = None) -> dict[str, Any]:
        """Add a code with a fixed number of uses. Raises IntegrityError when
        the normalized code already exists."""
        norm = _promo_norm(code)
        if not norm:
            raise ValueError("promo code cannot be empty")
        if int(max_uses) < 1:
            raise ValueError("promo code needs at least one use")
        with self._lock:
            self._conn.execute(
                "INSERT INTO promo_codes (code, max_uses, uses, active, note, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (norm, int(max_uses), 0, 1, note, _now_iso()))
            self._conn.commit()
        row = self.promo_code(norm)
        assert row is not None
        return row

    def promo_code(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM promo_codes WHERE code=?", (_promo_norm(code),)
            ).fetchone()
        return dict(r) if r else None

    def list_promo_codes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM promo_codes ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def promo_code_status(self, code: str, identity: str) -> dict[str, Any]:
        """Read-only availability check for the APPLY button. Does not spend
        a use; submit redeems atomically."""
        row = self.promo_code(code)
        if row is None:
            return {"ok": False, "code": "invalid",
                    "detail": "That promo code didn't work."}
        if not int(row.get("active") or 0) or int(row["uses"]) >= int(row["max_uses"]):
            return {"ok": False, "code": "exhausted",
                    "detail": "That code is all used up."}
        with self._lock:
            used = self._conn.execute(
                "SELECT 1 FROM promo_redemptions WHERE code_id=? AND identity=?",
                (int(row["id"]), identity)).fetchone()
        if used:
            return {"ok": False, "code": "used_by_identity",
                    "detail": "You've already used that code."}
        return {"ok": True, "code": "ok"}

    def redeem_promo_code(self, code: str, identity: str,
                          request_id: int | None = None) -> dict[str, Any]:
        """Atomically spend one use and record the identity. The guarded
        UPDATE is the lock: a concurrent last-use claim either wins or gets
        `exhausted`; the redemption PK rejects a repeat identity."""
        norm = _promo_norm(code)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM promo_codes WHERE code=?", (norm,)).fetchone()
            if row is None:
                return {"ok": False, "code": "invalid",
                        "detail": "That promo code didn't work."}
            cid = int(row["id"])
            if not int(row["active"] or 0) or int(row["uses"]) >= int(row["max_uses"]):
                return {"ok": False, "code": "exhausted",
                        "detail": "That code is all used up."}
            # Postgres runs autocommit, so the guarded UPDATE and the
            # redemption insert need one explicit transaction; otherwise a
            # duplicate-identity insert would leave the use spent.
            self._conn.begin()
            cur = self._conn.execute(
                "UPDATE promo_codes SET uses=uses+1 "
                "WHERE id=? AND active=1 AND uses<max_uses RETURNING uses",
                (cid,))
            new_row = cur.fetchone()
            if new_row is None:
                self._conn.rollback()
                return {"ok": False, "code": "exhausted",
                        "detail": "That code is all used up."}
            uses = int(new_row[0])
            try:
                self._conn.execute(
                    "INSERT INTO promo_redemptions (code_id, identity, "
                    "request_id, created_at) VALUES (?,?,?,?)",
                    (cid, identity, request_id, _now_iso()))
            except self.IntegrityError:
                self._conn.rollback()
                return {"ok": False, "code": "used_by_identity",
                        "detail": "You've already used that code."}
            if uses >= int(row["max_uses"]):
                self._conn.execute(
                    "UPDATE promo_codes SET active=0, retired_at=?, "
                    "retired_by='exhausted' WHERE id=?",
                    (_now_iso(), cid))
            self._conn.commit()
        return {"ok": True, "code": "ok", "id": cid, "uses": uses,
                "max_uses": int(row["max_uses"])}

    def release_promo_redemption(self, code: str, identity: str) -> bool:
        """Give a use back when the request it comped was rejected before it
        reached the line. Only an auto-exhausted code is re-activated; an
        operator-retired code stays retired."""
        norm = _promo_norm(code)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM promo_codes WHERE code=?", (norm,)).fetchone()
            if row is None:
                return False
            cid = int(row["id"])
            self._conn.begin()
            cur = self._conn.execute(
                "DELETE FROM promo_redemptions WHERE code_id=? AND identity=?",
                (cid, identity))
            if cur.rowcount == 0:
                self._conn.commit()
                return False
            self._conn.execute(
                "UPDATE promo_codes SET uses=CASE WHEN uses>0 THEN uses-1 ELSE 0 END "
                "WHERE id=?", (cid,))
            if row["retired_by"] == "exhausted":
                self._conn.execute(
                    "UPDATE promo_codes SET active=1, retired_at=NULL, "
                    "retired_by=NULL WHERE id=? AND uses<max_uses", (cid,))
            self._conn.commit()
        return True

    def retire_promo_code(self, code: str) -> bool:
        """Remove a code from circulation without deleting it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE promo_codes SET active=0, retired_at=?, "
                "retired_by='operator' WHERE code=?",
                (_now_iso(), _promo_norm(code)))
            self._conn.commit()
            return cur.rowcount > 0

    def activate_promo_code(self, code: str) -> bool:
        """Put an operator-retired code back into circulation while it still
        has uses left."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE promo_codes SET active=1, retired_at=NULL, "
                "retired_by=NULL WHERE code=? AND uses<max_uses",
                (_promo_norm(code),))
            self._conn.commit()
            return cur.rowcount > 0

    def promo_redemptions(self, code: str) -> list[dict[str, Any]]:
        row = self.promo_code(code)
        if row is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM promo_redemptions WHERE code_id=? ORDER BY created_at",
                (int(row["id"]),)).fetchall()
        return [dict(r) for r in rows]
