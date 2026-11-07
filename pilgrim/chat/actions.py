"""The moderation/admin executor (SHEEP_PEN.md §6 B.3).

One code path for every chat action, whoever asked for it (Layer B worker or an
operator admin route): validate → durable Postgres audit row → `events` stream
entry → `PUBLISH` → CloudWatch audit line. Actions are idempotent on a stable
`action_id` (action type + target + expiry/window), so a retried batch never
doubles a ban, while a later legitimate ban/unban has a different id.

Internal Pub/Sub envelopes may carry a target `user_id`/`ip_hash`; the web tier
filters them before fan-out, and no frame sent to a browser ever carries one
(SHEEP_PEN.md §0.7.6).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pilgrim.chat import protocol
from pilgrim.chat.audit import AuditLog, NullAudit
from pilgrim.chat.redis_store import ChatStore
from pilgrim.config import Clock, Config
from pilgrim.store import Store

log = logging.getLogger("radio.chat.actions")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _reason_key(reason: str | None) -> str:
    return hashlib.sha256((reason or "").encode("utf-8")).hexdigest()[:8]


class ChatActions:
    def __init__(self, cfg: Config, store: Store, cache: ChatStore,
                 audit: AuditLog | NullAudit | None = None,
                 clock: Clock | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.cache = cache
        self.audit = audit or NullAudit()
        self.clock = clock or Clock()

    # ------------------------------------------------------------ events
    async def _event(self, envelope: dict[str, Any]) -> None:
        try:
            await self.cache.xadd_event({
                "event": str(envelope.get("type") or ""),
                "payload": json.dumps(envelope, separators=(",", ":")),
            })
        except Exception as e:  # noqa: BLE001 - the durable write already happened
            log.warning("chat.event_failed", extra={"type": envelope.get("type"),
                                                    "error": type(e).__name__})
        try:
            await self.cache.publish(envelope)
        except Exception as e:  # noqa: BLE001 - resync/next load covers it
            log.warning("chat.publish_failed", extra={"type": envelope.get("type"),
                                                      "error": type(e).__name__})

    async def _audit(self, event: str, **fields: Any) -> None:
        try:
            await self.audit.awrite(event, **fields)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.audit_error", extra={"event": event,
                                                   "error": type(e).__name__})

    # ------------------------------------------------------------ delete
    async def delete_message(self, message_id: str, *, actor: str,
                             reason: str | None = None,
                             policy: str | None = None,
                             confidence: float | None = None,
                             severity: int | None = None) -> bool:
        """Tombstone a message: Postgres truth, deleted set, live event.

        Idempotent on `delete:{id}`: a retry republishes nothing and does not
        duplicate the audit row.
        """
        row = self.store.chat_message(message_id)
        if row is None:
            log.info("chat.moderation.unknown_message", extra={"id": message_id})
            return False
        inserted = self.store.insert_chat_moderation(
            action_id=f"delete:{message_id}", message_id=message_id,
            user_id=row.get("user_id"), action="delete", policy=policy,
            severity=severity, confidence=confidence, reason=reason,
            text=row.get("text"), decided_by=actor,
            created_at=_iso(self.clock.wall()))
        self.store.mark_chat_message_deleted(message_id, actor,
                                             now=_iso(self.clock.wall()))
        try:
            await self.cache.sadd("deleted", message_id)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.deleted_set_failed", extra={"id": message_id,
                                                          "error": type(e).__name__})
        if inserted:
            await self._event(protocol.deleted_frame(message_id))
            await self._audit("chat.message_deleted", id=message_id,
                              user_id=row.get("user_id"), actor=actor,
                              policy=policy, reason=reason, text=row.get("text"))
        return True

    # ------------------------------------------------------------ warn
    async def warn_user(self, user_id: str, reason: str, *, actor: str,
                        message_id: str | None = None) -> bool:
        action_id = f"warn:{user_id}:{message_id or ''}:{_reason_key(reason)}"
        inserted = self.store.insert_chat_moderation(
            action_id=action_id, message_id=message_id, user_id=user_id,
            action="warn", policy="harassment", severity=1, confidence=1.0,
            reason=reason[:200], text=None, decided_by=actor,
            created_at=_iso(self.clock.wall()))
        if inserted:
            await self._event({"type": "warned", "user_id": user_id,
                               "reason": reason[:200]})
            await self._audit("chat.user_warned", user_id=user_id,
                              message_id=message_id, actor=actor, reason=reason)
        return True

    # ------------------------------------------------------------ ban
    async def ban_user(self, user_id: str, reason: str, *, actor: str,
                       severity: int = 2, confidence: float = 1.0,
                       message_id: str | None = None,
                       until: float | None = None) -> float:
        """Ladder ban (SHEEP_PEN.md §B.3): 10 min → 1 h → 24 h by bans_30d."""
        now = self.clock.wall()
        if until is None:
            count = self.store.bans_since("user", user_id, _iso(now - 86400))
            ladder = self.cfg.chat.ban_ladder_s
            until = now + ladder[min(count, len(ladder) - 1)]
        action_id = f"ban:user:{user_id}:{int(until)}"
        inserted = self.store.insert_chat_moderation(
            action_id=action_id, message_id=message_id, user_id=user_id,
            action="ban_user", policy="harassment", severity=severity,
            confidence=confidence, reason=reason[:200], text=None,
            decided_by=actor, created_at=_iso(now))
        self.store.insert_chat_ban(scope="user", target=user_id, until=until,
                                   reason=reason[:200], created_at=_iso(now))
        try:
            await self.cache.set(f"ban:user:{user_id}",
                                 json.dumps({"until": until, "reason": reason[:200]}),
                                 ex=max(1, int(until - now)))
        except Exception as e:  # noqa: BLE001 - Postgres still holds the ban
            log.warning("chat.ban_key_failed", extra={"error": type(e).__name__})
        if inserted:
            await self._event({"type": "banned", "user_id": user_id, "until": until})
            await self._audit("chat.user_banned", user_id=user_id, until=until,
                              actor=actor, reason=reason, message_id=message_id)
            if message_id:
                await self.delete_message(message_id, actor=actor,
                                          reason="banned message")
            await self._maybe_ip_ban(user_id, actor=actor, reason=reason)
        return until

    async def _maybe_ip_ban(self, user_id: str, *, actor: str,
                            reason: str) -> None:
        """Deterministic escalation: N distinct banned users from one IP in
        24 h bans the IP for `ip_ban_s` (SHEEP_PEN.md §B.3)."""
        user = self.store.chat_user(user_id) or {}
        iph = user.get("ip_hash")
        if not iph:
            return
        try:
            key = f"ipbans:{iph}"
            await self.cache.sadd(key, user_id)
            await self.cache.expire(key, 86400)
            n = await self.cache.scard(key)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.ipban_counter_failed", extra={"error": type(e).__name__})
            return
        if n < self.cfg.chat.ip_ban_after_users:
            return
        now = self.clock.wall()
        until = now + self.cfg.chat.ip_ban_s
        action_id = f"ban:ip:{iph}:{int(until)}"
        inserted = self.store.insert_chat_moderation(
            action_id=action_id, message_id=None, user_id=None, action="ban_ip",
            policy="harassment", severity=3, confidence=1.0,
            reason=reason[:200], text=None, decided_by=actor,
            created_at=_iso(now))
        self.store.insert_chat_ban(scope="ip", target=iph, until=until,
                                   reason=reason[:200], created_at=_iso(now))
        with contextlib.suppress(Exception):
            await self.cache.set(f"ban:ip:{iph}",
                                 json.dumps({"until": until, "reason": reason[:200]}),
                                 ex=max(1, int(until - now)))
        if inserted:
            await self._event({"type": "banned", "ip_hash": iph, "until": until})
            await self._audit("chat.ip_banned", ip_hash=iph, until=until,
                              actor=actor, reason=reason)

    async def ban_ip(self, ip_hash: str, reason: str, *, actor: str,
                     seconds: int | None = None) -> float:
        """Operator IP ban. A duration is a temporary ban; no duration is a
        long-lived one (the admin's call, SHEEP_PEN.md §10)."""
        now = self.clock.wall()
        until = now + (seconds if seconds is not None else 10 * 365 * 86400)
        action_id = f"ban:ip:{ip_hash}:{int(until)}"
        inserted = self.store.insert_chat_moderation(
            action_id=action_id, message_id=None, user_id=None, action="ban_ip",
            policy="other", severity=3, confidence=1.0, reason=reason[:200],
            text=None, decided_by=actor, created_at=_iso(now))
        self.store.insert_chat_ban(scope="ip", target=ip_hash, until=until,
                                   reason=reason[:200], created_at=_iso(now))
        with contextlib.suppress(Exception):
            await self.cache.set(f"ban:ip:{ip_hash}",
                                 json.dumps({"until": until, "reason": reason[:200]}),
                                 ex=max(1, int(until - now)))
        if inserted:
            await self._event({"type": "banned", "ip_hash": ip_hash, "until": until})
            await self._audit("chat.ip_banned", ip_hash=ip_hash, until=until,
                              actor=actor, reason=reason)
        return until

    async def unban(self, scope: str, target: str, *, actor: str) -> int:
        n = self.store.deactivate_chat_ban(scope, target)
        key = f"ban:{scope}:{target}"
        with contextlib.suppress(Exception):
            await self.cache.delete(key)
        if n:
            await self._event({"type": "unbanned", "scope": scope,
                               "target": target})
            await self._audit("chat.unbanned", scope=scope, target=target,
                              actor=actor)
        return n

    # ------------------------------------------------------------ flag / allow
    async def flag(self, message_id: str, user_id: str | None, *, actor: str,
                   policy: str, severity: int, confidence: float,
                   reason: str) -> bool:
        inserted = self.store.insert_chat_moderation(
            action_id=f"flag:{message_id}:{policy}", message_id=message_id,
            user_id=user_id, action="flag", policy=policy, severity=severity,
            confidence=confidence, reason=reason[:200], text=None,
            decided_by=actor, created_at=_iso(self.clock.wall()))
        if inserted:
            self.store.set_chat_message_status(message_id, "flagged")
            await self._event({"type": "moderation_flag", "id": message_id,
                               "policy": policy, "severity": severity})
            await self._audit("chat.moderation_flag", id=message_id,
                              user_id=user_id, actor=actor, policy=policy,
                              severity=severity, reason=reason)
        return True

    async def allow(self, message_id: str, *, actor: str) -> None:
        self.store.set_chat_message_status(message_id, "accepted")
        self.store.insert_chat_moderation(
            action_id=f"allow:{message_id}", message_id=message_id,
            user_id=None, action="allow", policy=None, severity=None,
            confidence=None, reason=None, text=None, decided_by=actor,
            created_at=_iso(self.clock.wall()))

    # ------------------------------------------------------------ operator switches
    async def set_posting_disabled(self, disabled: bool, *, actor: str,
                                   ttl_s: int | None = None) -> None:
        if disabled:
            await self.cache.set("posting_disabled", "1", ex=ttl_s)
        else:
            await self.cache.delete("posting_disabled")
        await self._event({"type": "posting_disabled", "disabled": disabled})
        await self._audit("chat.posting_disabled", disabled=disabled, actor=actor)

    async def set_giles_paused(self, paused: bool, *, actor: str) -> None:
        if paused:
            await self.cache.set("giles:paused", "1")
        else:
            await self.cache.delete("giles:paused")
        await self._event({"type": "giles_paused", "paused": paused})
        await self._audit("chat.giles_paused", paused=paused, actor=actor)

    async def set_slow_mode(self, interval_s: float, *, actor: str) -> None:
        await self.cache.set("slow_mode", str(interval_s))
        await self._event(protocol.slow_mode_frame(interval_s))
        await self._audit("chat.slow_mode", interval_s=interval_s, actor=actor)

    async def clear_slow_mode(self, *, actor: str) -> None:
        await self.cache.delete("slow_mode")
        await self._audit("chat.slow_mode_cleared", actor=actor)
