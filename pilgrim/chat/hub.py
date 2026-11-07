"""Per-task WebSocket fan-out and the single Valkey subscriber.

SHEEP_PEN.md §0.7.4. One subscriber per task on the exact channel
`pilgrim:{chat}:pubsub` (serverless has no `PSUBSCRIBE`), a bounded
`asyncio.Queue` per socket (a full queue closes that socket with 1013 so the
client reconnects and replays), and an `XRANGE` resync every `chat.resync_s`
that covers publish failures, Pub/Sub gaps and cache failover with one loop.

Internal envelopes may name a target `user_id`/`ip_hash`; they are filtered
here and never forwarded. Frames sent to browsers carry no identity handles
(SHEEP_PEN.md §0.7.6).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from pilgrim.chat import protocol
from pilgrim.chat.redis_store import ChatStore
from pilgrim.config import Clock, Config

log = logging.getLogger("radio.chat.hub")


def frame_from_fields(entry_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return protocol.message_frame(
        id=entry_id, name=str(fields.get("name") or ""),
        text=str(fields.get("text") or ""),
        ts=float(fields.get("ts") or 0.0),
        giles=str(fields.get("user_id") or "") == "giles")


class Socket:
    def __init__(self, ws: Any, user_id: str, ip_hash: str | None,
                 maxsize: int) -> None:
        self.ws = ws
        self.user_id = user_id
        self.ip_hash = ip_hash
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self.misses = 0


class ChatHub:
    def __init__(self, cfg: Config, cache: ChatStore,
                 store: Any | None = None,
                 clock: Clock | None = None) -> None:
        self.cfg = cfg
        self.cache = cache
        self.store = store
        self.clock = clock or Clock()
        self.sockets: set[Socket] = set()
        self.last_id = "0-0"
        self._last_resync = 0.0
        self._task: asyncio.Task | None = None
        self._closing = False
        self.frames_out = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._subscriber_loop())
            await asyncio.sleep(0)   # let the task subscribe before the first frame

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self.close_all(1012)

    def add(self, sock: Socket) -> None:
        self.sockets.add(sock)

    def remove(self, sock: Socket) -> None:
        self.sockets.discard(sock)

    def count_for_ip(self, ip_hash: str | None) -> int:
        if not ip_hash:
            return 0
        return sum(1 for s in self.sockets if s.ip_hash == ip_hash)

    async def close_all(self, code: int = 1012) -> None:
        for sock in list(self.sockets):
            await self._close(sock, code)

    async def _close(self, sock: Socket, code: int) -> None:
        self.sockets.discard(sock)
        with contextlib.suppress(Exception):
            await sock.ws.close(code=code)

    # ------------------------------------------------------------ fan-out
    def deliver(self, frame: dict[str, Any], *, target_user: str | None = None,
                target_ip: str | None = None) -> None:
        payload = protocol.dumps(frame)
        for sock in list(self.sockets):
            if target_user is not None and sock.user_id != target_user:
                continue
            if target_ip is not None and sock.ip_hash != target_ip:
                continue
            try:
                sock.queue.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("chat.slow_client", extra={"user": sock.user_id})
                # Closing is async; schedule it and drop the socket now so a
                # stalled browser cannot pin a queue.
                self.sockets.discard(sock)
                asyncio.create_task(self._close(sock, 1013))
        self.frames_out += 1

    def broadcast(self, frame: dict[str, Any]) -> None:
        self.deliver(frame)

    # ------------------------------------------------------------ subscriber
    async def _subscriber_loop(self) -> None:
        backoff = 1.0
        while not self._closing:
            ps = None
            try:
                ps = await self.cache.pubsub()
                backoff = 1.0
                while not self._closing:
                    msg = await ps.get_message(timeout=self.cfg.chat.resync_s)
                    if msg and msg.get("type") == "message":
                        try:
                            envelope = json.loads(msg.get("data") or "{}")
                        except (TypeError, ValueError):
                            envelope = {}
                        if isinstance(envelope, dict):
                            self._dispatch(envelope)
                    now = self.clock.now()
                    if now - self._last_resync >= self.cfg.chat.resync_s:
                        await self._resync()
                        self._last_resync = now
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - resubscribe with backoff
                log.warning("chat.subscriber_error",
                            extra={"error": f"{type(e).__name__}: {e}"[:200]})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.chat.reconnect_max_s)
            finally:
                if ps is not None:
                    with contextlib.suppress(Exception):
                        await ps.aclose()

    def _dispatch(self, envelope: dict[str, Any]) -> None:
        kind = envelope.get("type")
        if kind == "message":
            entry_id = envelope.get("id")
            if isinstance(entry_id, str) and protocol.is_stream_id(entry_id):
                if protocol.stream_id_key(entry_id) <= protocol.stream_id_key(self.last_id):
                    return  # already delivered (live publish + resync overlap)
                self.last_id = entry_id
            self.broadcast(envelope)
        elif kind in ("deleted", "renamed", "slow_mode"):
            self.broadcast(envelope)
        elif kind == "warned":
            self.deliver(protocol.warned_frame(str(envelope.get("reason") or "")),
                         target_user=envelope.get("user_id"))
        elif kind == "banned":
            frame = protocol.banned_frame(float(envelope.get("until") or 0.0))
            if envelope.get("user_id"):
                self.deliver(frame, target_user=envelope["user_id"])
            elif envelope.get("ip_hash"):
                self.deliver(frame, target_ip=envelope["ip_hash"])
        # moderation_flag / posting_disabled / giles_paused / unbanned stay
        # internal: the admin surface reads them, browsers do not.

    async def _resync(self) -> None:
        try:
            rows = await self.cache.xrange_after(self.last_id,
                                                 count=self.cfg.chat.history_max)
        except Exception as e:  # noqa: BLE001
            log.warning("chat.replay_error", extra={"error": type(e).__name__})
            raise
        if not rows:
            return
        ids = [str(i) for i, _ in rows]
        deleted: set[str] = set()
        try:
            marks = await self.cache.smismember("deleted", ids)
            deleted = {i for i, m in zip(ids, marks, strict=True) if m}
        except Exception:  # noqa: BLE001 - fall back to Postgres below
            if self.store is not None:
                for i in ids:
                    row = self.store.chat_message(i)
                    if row is not None and row.get("status") == "deleted":
                        deleted.add(i)
        for entry_id, fields in rows:
            if entry_id in deleted:
                self.last_id = entry_id
                continue
            self.broadcast(frame_from_fields(str(entry_id), dict(fields)))
            self.last_id = str(entry_id)
        self._last_resync = self.clock.now()


async def history_frames_async(cache: ChatStore, store: Any,
                               limit: int) -> list[dict[str, Any]]:
    """Newest `limit` messages, oldest-first, tombstones applied.

    Valkey is the fast path; when the deleted set is unavailable the durable
    Postgres rows decide (SHEEP_PEN.md §0.7.5).
    """
    rows = await cache.recent(limit)
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
    return [frame_from_fields(str(i), dict(f))
            for i, f in reversed(rows) if str(i) not in deleted]

