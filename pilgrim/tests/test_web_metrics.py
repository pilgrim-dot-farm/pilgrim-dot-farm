"""pilgrim/web_metrics.py on the real web app (MONITORING.md Step 4). Fakes
only: the `_pair` helper from test_web_app (FakeRedis + the test store)."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from pilgrim import web_metrics as wm
from pilgrim.metrics import Registry
from pilgrim.web_app import create_web_app
from test_web_app import _pair


def _client(tmp_env, monkeypatch, *, publish=True):
    station, web, _, r = _pair(tmp_env, monkeypatch, publish=publish)
    reg = Registry()
    wm.declare(reg)
    app = create_web_app(web.cfg, web)
    wm.add_middleware(app, reg)
    return station, web, TestClient(app), reg


def test_requests_are_counted_by_route_template(tmp_env, monkeypatch):
    _, _, c, reg = _client(tmp_env, monkeypatch)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/station/state").status_code == 200
    assert c.post("/api/requests/987654/vote",
                  json={"voter_id": "0" * 32, "value": 1}).status_code in (404, 409, 422)
    assert c.get("/api/nope/secret-path").status_code == 404
    assert c.get("/").status_code == 200
    text = reg.render()
    assert ('pilgrim_web_http_requests_total{method="GET",route="/api/health",status="200"} 1\n'
            in text)
    assert 'route="/api/requests/{request_id}/vote"' in text
    assert 'route="/api/{rest:path}",status="404"' in text
    assert 'pilgrim_web_http_requests_total{method="GET",route="static",status="200"} 1\n' in text
    assert 'pilgrim_web_http_request_seconds_count{route="/api/health"} 1\n' in text
    assert "987654" not in text and "secret-path" not in text


def test_off_air_is_counted_as_503(tmp_env, monkeypatch):
    _, _, c, reg = _client(tmp_env, monkeypatch, publish=False)
    assert c.get("/api/station/program").status_code == 503
    assert 'route="/api/station/program",status="503"' in reg.render()


def test_refresh_reports_cache_and_station(tmp_env, monkeypatch):
    _, web, _, reg = _client(tmp_env, monkeypatch)
    asyncio.run(wm.refresh(reg, web))
    text = reg.render()
    assert "pilgrim_web_cache_up 1\n" in text and "pilgrim_web_station_on_air 1\n" in text
    assert "pilgrim_web_chat_sockets 0\n" in text
    assert "pilgrim_web_chat_worker_up 0\n" in text      # no chat cache in this fixture

    def boom(name):
        raise ConnectionError("cache down")
    monkeypatch.setattr(web.hot, "read", boom)
    asyncio.run(wm.refresh(reg, web))
    text = reg.render()
    assert "pilgrim_web_cache_up 0\n" in text and "pilgrim_web_station_on_air 0\n" in text


def test_chat_worker_heartbeat_is_reported(tmp_env, monkeypatch):
    """MONITORING §2.3: the chat worker is a separate process; its heartbeat
    key is the liveness signal, so a dead worker is visible from the web tier."""
    _, web, _, reg = _client(tmp_env, monkeypatch)

    class FakeChat:
        async def get(self, name):
            return b"1794000000.5" if name == "worker:heartbeat" else None

    web.chat_cache = FakeChat()
    asyncio.run(wm.refresh(reg, web))
    text = reg.render()
    assert "pilgrim_web_chat_worker_up 1\n" in text
    assert "pilgrim_web_chat_worker_heartbeat_timestamp_seconds 1794000000.5\n" in text

    class DeadChat:
        async def get(self, name):
            return None

    web.chat_cache = DeadChat()
    asyncio.run(wm.refresh(reg, web))
    assert "pilgrim_web_chat_worker_up 0\n" in reg.render()


def test_install_is_a_no_op_when_disabled(tmp_env, monkeypatch):
    _, web, _, _ = _client(tmp_env, monkeypatch)
    web.cfg.metrics.enabled = False
    app = create_web_app(web.cfg, web)
    before = len(app.user_middleware)
    assert wm.install(web.cfg, app, web, Registry()) is False
    assert len(app.user_middleware) == before
