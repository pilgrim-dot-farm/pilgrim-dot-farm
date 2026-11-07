"""The Sheep Pen API: REST name/history and the WebSocket accept path."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import NullAudit
from pilgrim.chat.hub import ChatHub
from pilgrim.chat.redis_store import ChatStore
from pilgrim.chat.routes import register_chat_routes
from pilgrim.config import Clock
from pilgrim.tests.fakes.fake_redis import FakeAsyncRedis

TOKEN_A = "0123456789abcdef0123456789abcdef"
TOKEN_B = "fedcba9876543210fedcba9876543210"


class _Station:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @staticmethod
    def _client_ip(conn):
        return "203.0.113.7"


def _app(cfg, store):
    clock = Clock()
    client = FakeAsyncRedis(time.time)
    cache = ChatStore(cfg, client)
    hub = ChatHub(cfg, cache, store, clock)
    audit = NullAudit()
    actions = ChatActions(cfg, store, cache, audit, clock)

    @asynccontextmanager
    async def lifespan(app):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(lifespan=lifespan)
    register_chat_routes(app, _Station(cfg), store, cache, hub, audit, clock)

    # Test-only hooks: run executor actions inside the app's event loop (the
    # fake Valkey is loop-bound, so a test must not await them from outside).
    @app.post("/api/test/chat/ban/{user_id}")
    async def _test_ban(user_id: str):
        until = await actions.ban_user(user_id, "abuse", actor="test",
                                       until=time.time() + 600)
        return {"ok": True, "until": until}

    @app.post("/api/test/chat/delete/{message_id}")
    async def _test_delete(message_id: str):
        return {"ok": await actions.delete_message(message_id, actor="test",
                                                   reason="test")}

    @app.post("/api/test/chat/posting")
    async def _test_posting(enabled: bool = True):
        await actions.set_posting_disabled(not enabled, actor="test")
        return {"ok": True}

    return app, cache, hub, actions


def _hello(ws, token, last_id=None):
    ws.send_text(json.dumps({"type": "hello", "token": token,
                             "last_id": last_id, "client_version": "test"}))
    return json.loads(ws.receive_text())


def _recv_until(ws, kind, limit=10):
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind} frame")


def _claim(client, token, name):
    return client.post("/api/chat/name", json={"token": token, "name": name})


def _send(ws, text, client_msg_id=None):
    ws.send_text(json.dumps({"type": "send", "text": text,
                             "client_msg_id": client_msg_id or
                             f"msg-{time.time_ns()}"}))


def test_welcome_is_read_only_before_a_name(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client, client.websocket_connect("/api/chat/ws") as ws:
        welcome = _hello(ws, TOKEN_A)
        assert welcome["type"] == "welcome"
        assert welcome["name"] is None
        assert welcome["history"] == []
        _send(ws, "hello")
        err = _recv_until(ws, "error")
        assert err["code"] == "name_required"


def test_claim_then_send_and_receive(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        r = _claim(client, TOKEN_A, "Ada")
        assert r.status_code == 200 and r.json()["name"] == "Ada"
        assert r.headers["cache-control"] == "no-store"
        with client.websocket_connect("/api/chat/ws") as ws:
            welcome = _hello(ws, TOKEN_A)
            assert welcome["name"] == "Ada"
            _send(ws, "hello pen", "cid-0001")
            ack = _recv_until(ws, "ack")
            assert ack["client_msg_id"] == "cid-0001"
            msg = _recv_until(ws, "message")
            assert msg["text"] == "hello pen" and msg["name"] == "Ada"
            assert msg["id"] == ack["id"]
            # only the pseudonym is stored, never the raw IP
            user = store.chat_user_by_token(hashlib.sha256(TOKEN_A.encode()).hexdigest())
            assert user["ip_hash"] == hashlib.sha256(b"203.0.113.7").hexdigest()[:16]


def test_two_participants_share_the_room(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        assert _claim(client, TOKEN_A, "Ada").status_code == 200
        assert _claim(client, TOKEN_B, "Bram").status_code == 200
        with client.websocket_connect("/api/chat/ws") as a, \
                client.websocket_connect("/api/chat/ws") as b:
            _hello(a, TOKEN_A)
            _hello(b, TOKEN_B)
            _send(a, "from ada")
            assert _recv_until(a, "ack")["id"]
            assert _recv_until(a, "message")["text"] == "from ada"
            assert _recv_until(b, "message")["text"] == "from ada"
            _send(b, "from bram")
            assert _recv_until(b, "ack")["id"]
            assert _recv_until(a, "message")["text"] == "from bram"


def test_duplicate_client_msg_id_is_idempotent(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            _send(ws, "once", "same-id-0001")
            first = _recv_until(ws, "ack")
            _recv_until(ws, "message")
            _send(ws, "once", "same-id-0001")
            second = _recv_until(ws, "ack")
            assert first["id"] == second["id"]
            # no second message frame is broadcast
            ws.send_text(json.dumps({"type": "ping"}))
            assert _recv_until(ws, "pong")
            assert len(store.recent_chat_messages(10)) == 1


def test_rate_limit_is_central(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            _send(ws, "one", "client-01")
            _recv_until(ws, "ack")
            _recv_until(ws, "message")
            _send(ws, "two", "client-02")
            assert _recv_until(ws, "error")["code"] == "rate"


def test_banned_user_gets_a_banned_frame(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, actions = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        user = store.chat_user_by_token(hashlib.sha256(TOKEN_A.encode()).hexdigest())
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            r = client.post(f"/api/test/chat/ban/{user['user_id']}")
            assert r.status_code == 200
            frame = _recv_until(ws, "banned")
            assert frame["until"] > time.time()
            _send(ws, "still here", "client-01")
            assert _recv_until(ws, "banned")


def test_deleted_message_propagates(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, actions = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            _send(ws, "delete me", "client-01")
            ack = _recv_until(ws, "ack")
            _recv_until(ws, "message")
            r = client.post(f"/api/test/chat/delete/{ack['id']}")
            assert r.status_code == 200
            assert _recv_until(ws, "deleted")["id"] == ack["id"]


def test_posting_disabled(cfg, tmp_env):
    _, store, _ = tmp_env
    app, cache, _, actions = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            assert client.post("/api/test/chat/posting?enabled=false").status_code == 200
            _send(ws, "hello", "client-01")
            assert _recv_until(ws, "error")["code"] == "posting_disabled"


def test_history_replay_after_reconnect(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        _claim(client, TOKEN_A, "Ada")
        with client.websocket_connect("/api/chat/ws") as ws:
            _hello(ws, TOKEN_A)
            _send(ws, "remember this", "client-01")
            ack = _recv_until(ws, "ack")
        # a new socket replays from the last id it saw
        with client.websocket_connect("/api/chat/ws") as ws:
            welcome = _hello(ws, TOKEN_A, last_id="0-0")
            ids = [m["id"] for m in welcome["history"]]
            assert ack["id"] in ids
        r = client.get("/api/chat/history", params={"after": "0-0"})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert r.json()["messages"][-1]["id"] == ack["id"]


def test_name_conflict_and_reserved(cfg, tmp_env):
    _, store, _ = tmp_env
    app, _, _, _ = _app(cfg, store)
    with TestClient(app) as client:
        assert _claim(client, TOKEN_A, "Thistle").status_code == 200
        r = _claim(client, TOKEN_B, "thistle")
        assert r.status_code == 409 and r.json()["code"] == "name_taken"
        assert _claim(client, TOKEN_B, "Giles").status_code == 400
