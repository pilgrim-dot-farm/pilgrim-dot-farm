"""The Fargate web tier's metrics (MONITORING.md §2; RADIO §11 "Metrics").

Request counts and latency per route template, what this task sees in the
cache, its open chat sockets, and whether the separate chat worker process is
alive (its `worker:heartbeat` key). Served on `metrics.port`, a second socket
that is never registered with the ALB (terraform: the target group knows only
`station.port`). `install()` is called from `web_app.main()` only, so tests
and `create_web_app` are unchanged unless a test installs it itself.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import Response

from pilgrim.config import Config
from pilgrim.metrics import REGISTRY, Registry, install_log_metrics, serve

log = logging.getLogger("radio.metrics")

_tasks: list[asyncio.Task] = []

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def declare(reg: Registry) -> None:
    reg.gauge("pilgrim_process_start_timestamp_seconds", "When this process declared its metrics.")
    reg.counter("pilgrim_web_http_requests_total",
                "HTTP requests by route template, method and status.")
    reg.histogram("pilgrim_web_http_request_seconds",
                  "HTTP request wall time by route template.", HTTP_BUCKETS)
    reg.gauge("pilgrim_web_cache_up", "1 when this task can read the hot state from Valkey.")
    reg.gauge("pilgrim_web_station_on_air", "1 when the published health says on air.")
    reg.gauge("pilgrim_web_chat_sockets", "Open chat WebSockets on this task.")
    reg.gauge("pilgrim_web_chat_worker_up",
              "1 when the chat worker's heartbeat key is present.")
    reg.gauge("pilgrim_web_chat_worker_heartbeat_timestamp_seconds",
              "The chat worker's last heartbeat; 0 when none.")
    reg.gauge("pilgrim_metrics_refresh_timestamp_seconds", "When refresh() last succeeded.")
    reg.counter("pilgrim_metrics_refresh_failures_total", "refresh() raised.")
    reg.set("pilgrim_process_start_timestamp_seconds", time.time())
    reg.inc("pilgrim_metrics_refresh_failures_total", 0)


def route_label(request: Request) -> str:
    """The matched route's template (`/api/requests/{request_id}/vote`), so a
    path parameter never becomes a label value. The static mount and anything
    unmatched are one value each."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path.startswith("/api/"):
        return path
    return "static"


def add_middleware(app: FastAPI, reg: Registry) -> None:
    @app.middleware("http")
    async def _metrics(request: Request,
                       call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = route_label(request)
            reg.inc("pilgrim_web_http_requests_total", route=route,
                    method=request.method, status=str(status))
            reg.observe("pilgrim_web_http_request_seconds", time.perf_counter() - t0, route=route)


def read_cache(station: Any) -> tuple[bool, bool]:
    """(cache reachable, station on air). One Valkey read, in a worker thread."""
    try:
        doc = station.hot.read("health")
    except Exception:  # noqa: BLE001 - a cache outage is the signal
        return False, False
    return True, isinstance(doc, dict) and bool(doc.get("on_air"))


async def read_worker(reg: Registry, station: Any) -> None:
    """The chat worker runs in its own launchd process (`pilgrim/chat_worker.py`)
    and writes `worker:heartbeat` to Valkey every 30 s with a 120 s TTL. Its
    presence is the liveness signal; a missing key is a dead worker, not a
    missing metric (MONITORING §2.3)."""
    cache = getattr(station, "chat_cache", None)
    value: float | None = None
    if cache is not None:
        try:
            beat = await cache.get("worker:heartbeat")
        except Exception:  # noqa: BLE001 - a cache outage is the signal
            beat = None
        if isinstance(beat, bytes | bytearray):
            beat = beat.decode("utf-8", "replace")
        try:
            value = float(beat)
        except (TypeError, ValueError):
            value = None
    reg.set("pilgrim_web_chat_worker_up", value is not None)
    reg.set("pilgrim_web_chat_worker_heartbeat_timestamp_seconds", value or 0)


async def refresh(reg: Registry, station: Any) -> None:
    up, on_air = await asyncio.to_thread(read_cache, station)
    reg.set("pilgrim_web_cache_up", up)
    reg.set("pilgrim_web_station_on_air", on_air)
    hub = getattr(station, "chat_hub", None)
    reg.set("pilgrim_web_chat_sockets", len(hub.sockets) if hub is not None else 0)
    await read_worker(reg, station)
    reg.set("pilgrim_metrics_refresh_timestamp_seconds", time.time())


async def run(reg: Registry, station: Any, cfg: Config) -> None:
    while True:
        try:
            await refresh(reg, station)
        except Exception as e:  # noqa: BLE001
            reg.inc("pilgrim_metrics_refresh_failures_total")
            log.warning("metrics.refresh_failed", extra={"error": f"{type(e).__name__}: {e}"[:300]})
        await asyncio.sleep(cfg.metrics.live_s)


def install(cfg: Config, app: FastAPI, station: Any, reg: Registry = REGISTRY) -> bool:
    """Called once from `web_app.main()` before uvicorn starts. Adds the
    request middleware now and starts the server + loop at app startup."""
    if not cfg.metrics.enabled:
        return False
    declare(reg)
    install_log_metrics(reg, station_histograms=False)
    add_middleware(app, reg)

    @app.on_event("startup")
    async def _metrics_start() -> None:
        try:
            serve(reg, cfg.metrics.host, cfg.metrics.port)
        except OSError as e:
            log.warning("metrics.disabled", extra={"error": f"{type(e).__name__}: {e}"[:300]})
            return
        _tasks.append(asyncio.create_task(run(reg, station, cfg)))
        log.info("metrics.ready", extra={"host": cfg.metrics.host, "port": cfg.metrics.port})

    return True
