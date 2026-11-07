"""The Sheep Pen HTTP + WebSocket surface (SHEEP_PEN.md §0.7.2–7; RADIO §11).

Registered from `web_app.create_web_app` and the fakes e2e server, before the
HTTP-only `_add_api_404` catch-all. The Mac station process serves none of
these: it serves `/api/admin/chat/*` only.

Accept path (SHEEP_PEN.md §0.7.3): Layer A → ban checks → rate limits → XADD →
Postgres insert → PUBLISH → audit → ack. A message is durable before it is
announced; a failed publish is covered by the subscriber's resync.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pilgrim.chat import identity, protocol
from pilgrim.chat.audit import AuditLog, NullAudit
from pilgrim.chat.hub import ChatHub, Socket
from pilgrim.chat.redis_store import ChatStore
from pilgrim.chat.screen import screen_text
from pilgrim.config import Clock, Config
from pilgrim.store import Store

log = logging.getLogger("radio.chat.routes")


class NameIn(BaseModel):
    token: str = Field(min_length=32, max_length=32)
    name: str = Field(min_length=1, max_length=80)


class _Ban(Exception):
    def __init__(self, until: float) -> None:
        super().__init__("banned")
        self.until = until


async def _ban_until(cache: ChatStore, store: Store, scope: str, target: str,
                     wall: float) -> float | None:
    """Ban expiry from Valkey, falling back to Postgres when the key is gone
    (cache failover must not unban anyone; SHEEP_PEN.md §9)."""
    try:
        raw = await cache.get(f"ban:{scope}:{target}")
        if raw:
            doc = json.loads(raw) if isinstance(raw, str) else raw
            until = float(doc.get("until") or 0.0)
            if until > wall:
                return until
    except Exception:  # noqa: BLE001
        pass
    row = store.active_chat_ban(scope, target, wall)
    return float(row["until"]) if row else None


async def _slow_mode(cache: ChatStore) -> float | None:
    try:
        raw = await cache.get("slow_mode")
        return float(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def _rows_to_frames(rows: list[Any], cache: ChatStore,
                          store: Store) -> list[dict[str, Any]]:
    ids = [str(i) for i, _ in rows]
    deleted: set[str] = set()
    try:
        marks = await cache.smismember("deleted", ids)
        deleted = {i for i, m in zip(ids, marks, strict=True) if m}
    except Exception:  # noqa: BLE001
        for i in ids:
            row = store.chat_message(i)
            if row is not None and row.get("status") == "deleted":
                deleted.add(i)
    from pilgrim.chat.hub import frame_from_fields

    return [frame_from_fields(str(i), dict(f)) for i, f in rows
            if str(i) not in deleted]


def register_chat_routes(app: FastAPI, station: Any, store: Store,
                         cache: ChatStore, hub: ChatHub,
                         audit: AuditLog | NullAudit | None = None,
                         clock: Clock | None = None) -> None:
    cfg: Config = station.cfg
    audit = audit or NullAudit()
    clock = clock or Clock()

    # ------------------------------------------------------------ REST
    @app.post("/api/chat/name")
    async def claim(payload: NameIn, request: Request):
        if not identity.valid_token(payload.token):
            raise HTTPException(400, detail={"code": "token", "detail": "bad browser token"})
        ip = station._client_ip(request)
        user = await identity.resolve_user(store, cache, payload.token, ip)
        try:
            updated = await identity.claim_name(store, cache, cfg, user,
                                                payload.name, epoch=clock.wall())
        except identity.NameError_ as e:
            status = 409 if e.code in ("name_taken", "name_locked") else 400
            return JSONResponse(status_code=status,
                                content={"ok": False, "code": e.code},
                                headers={"Cache-Control": "no-store"})
        await audit.awrite("chat.name_claimed", user_id=updated["user_id"],
                           ip_hash=identity.ip_hash(ip), name=updated.get("name"))
        return JSONResponse({"ok": True, "name": updated.get("name")},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/chat/history")
    async def history(after: str | None = Query(default=None),
                      limit: int = Query(default=50, ge=1, le=200)):
        if after is not None and not protocol.is_stream_id(after):
            raise HTTPException(400, detail={"code": "bad_cursor",
                                             "detail": "after must be a stream id"})
        if after:
            rows = await cache.xrange_after(after, count=limit)
        else:
            rows = list(reversed(await cache.recent(limit)))
        frames = await _rows_to_frames(rows, cache, store)
        last_id = frames[-1]["id"] if frames else (after or hub.last_id)
        return JSONResponse({"messages": frames, "last_id": last_id},
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ socket
    @app.websocket("/api/chat/ws")
    async def chat_ws(ws: WebSocket):
        await ws.accept()
        ip = station._client_ip(ws)
        iph = identity.ip_hash(ip)
        # ASGI sends are not concurrency-safe: the reader (acks/errors) and the
        # writer (fan-out queue) share one lock.
        send_lock = asyncio.Lock()

        async def send(frame: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_text(protocol.dumps(frame))

        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except (TimeoutError, WebSocketDisconnect):
            await ws.close(code=1008)
            return
        try:
            hello = protocol.parse_client_frame(raw)
        except ValueError:
            await send(protocol.error_frame("bad_frame", "expected hello"))
            await ws.close(code=1008)
            return
        if not isinstance(hello, protocol.Hello) or not identity.valid_token(hello.token):
            await send(protocol.error_frame("token", "bad browser token"))
            await ws.close(code=1008)
            return

        user = await identity.resolve_user(store, cache, hello.token, ip)
        uid = user["user_id"]
        wall = clock.wall()

        if hub.count_for_ip(iph) >= cfg.chat.rate_conn_per_ip:
            await send(protocol.error_frame("conn_rate", "too many connections"))
            await ws.close(code=1013)
            return
        try:
            if iph:
                await cache.incr_with_ttl(f"rate:conn:{iph}", 60)
        except Exception:  # noqa: BLE001 - a metric, not a gate
            pass

        sock = Socket(ws, uid, iph, cfg.chat.client_queue_max)
        hub.add(sock)
        try:
            banned_until = None
            if iph:
                banned_until = await _ban_until(cache, store, "ip", iph, wall)
            if banned_until is None:
                banned_until = await _ban_until(cache, store, "user", uid, wall)
            slow = await _slow_mode(cache)
            rows = await cache.recent(50)
            history = await _rows_to_frames(list(reversed(rows)), cache, store)
            await send(protocol.welcome_frame(
                name=user.get("name"), banned_until=banned_until,
                slow_mode=slow, last_id=hub.last_id, history=history))

            async def accept_send(frame: protocol.Send) -> None:
                await _accept_send(frame, user, sock, iph, cache, store, hub,
                                   cfg, audit, clock, send)

            async def reader() -> None:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.receive_text(),
                                                     timeout=cfg.chat.ping_s)
                    except TimeoutError:
                        sock.misses += 1
                        if sock.misses >= 2:
                            await ws.close(code=1001)
                            return
                        await send(protocol.ping_frame())
                        continue
                    except WebSocketDisconnect:
                        return
                    except Exception:  # noqa: BLE001 - socket already gone
                        return
                    sock.misses = 0
                    try:
                        frame = protocol.parse_client_frame(raw)
                    except ValueError:
                        await send(protocol.error_frame("bad_frame", "unreadable frame"))
                        continue
                    if isinstance(frame, protocol.Pong):
                        continue
                    if isinstance(frame, protocol.Ping):
                        await send(protocol.pong_frame())
                    elif isinstance(frame, protocol.NameFrame):
                        try:
                            updated = await identity.claim_name(
                                store, cache, cfg, user, frame.name,
                                epoch=clock.wall())
                            user.update(updated)
                            await send(protocol.renamed_frame("you",
                                                              updated.get("name") or ""))
                        except identity.NameError_ as e:
                            status = e.code
                            detail = ("that name is taken" if status == "name_taken"
                                      else "your name is locked" if status == "name_locked"
                                      else "that name can't be used")
                            await send(protocol.error_frame(status, detail))
                    elif isinstance(frame, protocol.Send):
                        await accept_send(frame)

            async def writer() -> None:
                while True:
                    payload = await sock.queue.get()
                    async with send_lock:
                        await ws.send_text(payload)

            tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                try:
                    exc = t.exception()
                except asyncio.CancelledError:
                    exc = None
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    log.info("chat.socket_closed", extra={"error": type(exc).__name__})
        finally:
            hub.remove(sock)
            with contextlib.suppress(Exception):
                await ws.close()


async def _accept_send(frame: protocol.Send, user: dict, sock: Socket,
                       iph: str | None, cache: ChatStore, store: Store,
                       hub: ChatHub, cfg: Config,
                       audit: AuditLog | NullAudit, clock: Clock,
                       send: Any) -> None:
    """One message through the accept path. Nothing is written until Layer A,
    bans and rate limits pass (SHEEP_PEN.md §0.7.3)."""
    uid = user["user_id"]
    wall = clock.wall()
    # The socket may have connected before the name claim (the client connects
    # on page load). Re-read the durable row so a claim that happened after the
    # hello is visible to this send.
    fresh = store.chat_user(uid) or user
    user = fresh

    async def reject(code: str, detail: str, text: str | None = None) -> None:
        await send(protocol.error_frame(code, detail))
        await audit.awrite("chat.message_rejected", code=code, user_id=uid,
                           ip_hash=iph, text=text)

    if await cache.exists("posting_disabled"):
        await reject("posting_disabled", "The pen is closed for a moment.")
        return
    if not user.get("name"):
        await reject("name_required", "Pick a name first.")
        return

    try:
        if iph:
            until = await _ban_until(cache, store, "ip", iph, wall)
            if until:
                raise _Ban(until)
        until = await _ban_until(cache, store, "user", uid, wall)
        if until:
            raise _Ban(until)
    except _Ban as b:
        await send(protocol.banned_frame(b.until))
        return

    sent_key = f"sent:{uid}:{frame.client_msg_id}"
    try:
        existing = await cache.get(sent_key)
    except Exception:  # noqa: BLE001
        existing = None
    if existing:
        await send(protocol.ack_frame(frame.client_msg_id, str(existing)))
        return

    text = frame.text.strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    repeated = False
    try:
        last = await cache.get_json(f"last:{uid}")
        if isinstance(last, dict) and last.get("h") == digest:
            repeated = wall - float(last.get("ts") or 0.0) < cfg.chat.repeat_window_s
    except Exception:  # noqa: BLE001
        last = None

    screen = screen_text(frame.text, cfg.chat, repeated=repeated)
    if not screen.ok:
        await reject(screen.code or "invalid", screen.detail, frame.text)
        return

    slow = await _slow_mode(cache)
    window = int(max(cfg.chat.rate_user_s, slow or 0.0))
    try:
        n_user = await cache.incr_with_ttl(f"rate:user:{uid}", window)
        n_ip = (await cache.incr_with_ttl(f"rate:ip:{iph}", 60)) if iph else 0
    except Exception as e:  # noqa: BLE001 - fail closed on the limiter
        log.warning("chat.rate_limiter_error", extra={"error": type(e).__name__})
        await reject("retry", "The pen is busy, try again.")
        return
    if n_user > 1 or n_ip > cfg.chat.rate_ip_per_min:
        await send(protocol.error_frame("rate", "Easy there — give it a moment."))
        await audit.awrite("chat.rate_limited", user_id=uid, ip_hash=iph)
        return

    fields = {"user_id": uid, "name": str(user.get("name") or ""),
              "text": screen.text, "ts": str(wall),
              "client_msg_id": frame.client_msg_id,
              "flags": ",".join(screen.flags)}
    try:
        entry_id = await cache.xadd_message(fields, cfg.chat.history_max)
    except Exception as e:  # noqa: BLE001
        log.warning("chat.xadd_failed", extra={"error": type(e).__name__})
        await reject("retry", "The pen is busy, try again.", screen.text)
        return

    ok = store.insert_chat_message(
        message_id=entry_id, user_id=uid, name=str(user.get("name") or ""),
        text=screen.text, ts=wall, client_msg_id=frame.client_msg_id,
        status="pending_review", flags=",".join(screen.flags))
    if not ok:
        with contextlib.suppress(Exception):
            await cache.xdel(entry_id)
        await reject("retry", "The pen is busy, try again.", screen.text)
        return

    try:
        await cache.set_nx(sent_key, entry_id, ex=300)
        await cache.set_json(f"last:{uid}", {"h": digest, "ts": wall},
                             ex=int(cfg.chat.repeat_window_s) + 1)
    except Exception:  # noqa: BLE001
        pass

    message = protocol.message_frame(id=entry_id, name=str(user.get("name") or ""),
                                     text=screen.text, ts=wall, giles=False)
    try:
        await cache.publish(message)
    except Exception as e:  # noqa: BLE001 - durable already; resync delivers
        log.warning("chat.publish_failed", extra={"id": entry_id,
                                                  "error": type(e).__name__})
    await audit.awrite("chat.message_accepted", id=entry_id, user_id=uid,
                       ip_hash=iph, flags=screen.flags, text=screen.text)
    await send(protocol.ack_frame(frame.client_msg_id, entry_id))
