"""The Sheep Pen tables (RADIO §14; docs/shipped/sheep-pen.md).
"""
from __future__ import annotations

from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    _now_iso,
)


class ChatStore(StoreBase):
    # ---------------------------------------------------------------- chat
    # The Sheep Pen (docs/shipped/sheep-pen.md; RADIO §14). Valkey is the hot path; these
    # are the durable audit rows. `chat_messages.id` is the Valkey stream id.
    def create_chat_user(self, *, token_hash: str, ip_hash: str | None = None,
                         user_id: str | None = None,
                         created_at: str | None = None) -> dict[str, Any]:
        import uuid

        uid = user_id or uuid.uuid4().hex[:16]
        now = created_at or _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_users (user_id, token_hash, ip_hash, created_at, "
                "last_seen) VALUES (?,?,?,?,?)",
                (uid, token_hash, ip_hash, now, now))
            self._conn.commit()
        return self.chat_user(uid) or {}

    def chat_user_by_token(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM chat_users WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(r) if r else None

    def chat_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM chat_users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    def chat_user_by_name(self, name_norm: str) -> dict[str, Any] | None:
        """The active holder of a normalized name (uniqueness authority)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM chat_users WHERE name_norm=? AND released_at IS NULL",
                (name_norm,)).fetchone()
        return dict(r) if r else None

    def claim_chat_name(self, user_id: str, name: str, name_norm: str,
                        now: str | None = None) -> tuple[bool, dict[str, Any] | None]:
        """Claim or change a display name. First active claim wins; a taken
        name returns (False, None) and the caller answers 409 without detail.
        The partial unique index is the authority; the lock keeps the check
        and the write together on the one store."""
        now = now or _now_iso()
        with self._lock:
            holder = self._conn.execute(
                "SELECT user_id FROM chat_users WHERE name_norm=? AND released_at IS NULL",
                (name_norm,)).fetchone()
            if holder is not None and holder[0] != user_id:
                return False, None
            try:
                self._conn.execute(
                    "UPDATE chat_users SET name=?, name_norm=?, name_changed_at=?, "
                    "last_seen=? WHERE user_id=?",
                    (name, name_norm, now, now, user_id))
                self._conn.commit()
            except self._conn.IntegrityError:  # a race the lock cannot see
                self._conn.rollback()
                return False, None
            r = self._conn.execute(
                "SELECT * FROM chat_users WHERE user_id=?", (user_id,)).fetchone()
        return True, (dict(r) if r else None)

    def release_chat_name(self, user_id: str, now: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chat_users SET name=NULL, name_norm=NULL, released_at=? "
                "WHERE user_id=?", (now or _now_iso(), user_id))
            self._conn.commit()

    def touch_chat_user(self, user_id: str, now: str | None = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE chat_users SET last_seen=? WHERE user_id=?",
                               (now or _now_iso(), user_id))
            self._conn.commit()

    def release_stale_chat_names(self, before_iso: str, now: str | None = None) -> int:
        """Release names of users not seen since `before_iso` (worker sweep)."""
        now = now or _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chat_users SET name=NULL, name_norm=NULL, released_at=? "
                "WHERE released_at IS NULL AND name_norm IS NOT NULL "
                "AND (last_seen IS NULL OR last_seen < ?)", (now, before_iso))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def insert_chat_message(self, *, message_id: str, user_id: str, name: str,
                            text: str | None, ts: float, client_msg_id: str | None,
                            status: str = "pending_review", flags: str | None = None) -> bool:
        """Durable copy of an accepted message. False on a duplicate id (the
        accept path XDELs the stream entry and answers `retry`)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO chat_messages (id, user_id, name, text, ts, "
                    "client_msg_id, status, flags) VALUES (?,?,?,?,?,?,?,?)",
                    (message_id, user_id, name, text, ts, client_msg_id, status, flags))
                self._conn.commit()
                return True
            except self._conn.IntegrityError:
                self._conn.rollback()
                return False

    def chat_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM chat_messages WHERE id=?", (message_id,)).fetchone()
        return dict(r) if r else None

    def mark_chat_message_deleted(self, message_id: str, deleted_by: str,
                                  now: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chat_messages SET status='deleted', deleted_at=?, deleted_by=? "
                "WHERE id=?", (now or _now_iso(), deleted_by, message_id))
            self._conn.commit()

    def set_chat_message_status(self, message_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE chat_messages SET status=? WHERE id=?",
                               (status, message_id))
            self._conn.commit()

    def recent_chat_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first; text-exposing callers apply tombstones."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_messages ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def pending_chat_messages(self, limit: int = 200) -> list[dict[str, Any]]:
        """Oldest-first unreviewed rows: the recovery path when the Valkey
        stream has trimmed past the moderation cursor (docs/shipped/sheep-pen.md §B.4)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_messages WHERE status='pending_review' "
                "ORDER BY ts ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def purge_chat_text_before(self, cutoff_epoch: float,
                               cutoff_iso: str) -> tuple[int, int]:
        """Null message text older than the retention window; the tombstone /
        decision rows stay so replay still suppresses deleted content."""
        with self._lock:
            m = self._conn.execute(
                "UPDATE chat_messages SET text=NULL WHERE text IS NOT NULL AND ts < ?",
                (cutoff_epoch,))
            d = self._conn.execute(
                "UPDATE chat_moderation SET text=NULL WHERE text IS NOT NULL "
                "AND created_at < ?", (cutoff_iso,))
            self._conn.commit()
            return int(m.rowcount or 0), int(d.rowcount or 0)

    def insert_chat_ban(self, *, scope: str, target: str, until: float,
                        reason: str | None = None,
                        created_at: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_bans (scope, target, until, created_at, reason, "
                "active) VALUES (?,?,?,?,?,1)",
                (scope, target, until, created_at or _now_iso(), reason))
            self._conn.commit()

    def active_chat_ban(self, scope: str, target: str,
                        now_epoch: float) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM chat_bans WHERE scope=? AND target=? AND active=1 "
                "AND until > ? ORDER BY until DESC LIMIT 1",
                (scope, target, now_epoch)).fetchone()
        return dict(r) if r else None

    def deactivate_chat_ban(self, scope: str, target: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chat_bans SET active=0 WHERE scope=? AND target=? AND active=1",
                (scope, target))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def bans_since(self, scope: str, target: str, since_iso: str) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM chat_bans WHERE scope=? AND target=? "
                "AND created_at >= ?", (scope, target, since_iso)).fetchone()
        return int(r[0] if r else 0)

    def insert_chat_moderation(self, *, action_id: str, message_id: str | None,
                               user_id: str | None, action: str, policy: str | None,
                               severity: int | None, confidence: float | None,
                               reason: str | None, text: str | None,
                               decided_by: str,
                               created_at: str | None = None) -> bool:
        """Idempotent on `action_id`: a retry of the same action is a no-op."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO chat_moderation (action_id, message_id, user_id, "
                    "action, policy, severity, confidence, reason, text, created_at, "
                    "decided_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (action_id, message_id, user_id, action, policy, severity,
                     confidence, reason, text, created_at or _now_iso(), decided_by))
                self._conn.commit()
                return True
            except self._conn.IntegrityError:
                self._conn.rollback()
                return False

    def recent_chat_moderation(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_moderation ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def chat_moderation_for(self, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_moderation WHERE message_id=? ORDER BY id",
                (message_id,)).fetchall()
        return [dict(r) for r in rows]

    def chat_messages_since(self, user_id: str, since_epoch: float) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE user_id=? AND ts >= ?",
                (user_id, since_epoch)).fetchone()
        return int(r[0] if r else 0)

    def chat_moderation_count(self, user_id: str, action: str,
                              since_iso: str) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM chat_moderation WHERE user_id=? AND action=? "
                "AND created_at >= ?", (user_id, action, since_iso)).fetchone()
        return int(r[0] if r else 0)
