"""In-process Prometheus metrics (MONITORING.md; RADIO §11 "Metrics").

A tiny registry, the text exposition format, a logging handler that turns the
station's dotted log events into counters, and a stdlib HTTP server on its own
port. No new dependency (AGENTS §3). This module imports nothing from the
station: `station_metrics.py` and `web_metrics.py` fill the registry.

Thread safety: every read and write takes the registry lock. Values are
written on the event loop thread (and by the log handler from any thread) and
rendered by the metrics server thread.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("radio.metrics")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# MONITORING §6 rule 3: a label may never carry one of these.
FORBIDDEN_LABELS = frozenset({
    "player_id", "voter_id", "user_id", "ip", "ip_hash", "email", "token",
    "text", "title", "url", "request_id", "item_id"})

_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_EVENT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_%]+)+$")

Key = tuple[tuple[str, str], ...]


def _key(labels: dict[str, Any]) -> Key:
    for name in labels:
        if name in FORBIDDEN_LABELS or not _NAME.match(name):
            raise ValueError(f"metric label {name!r} is not allowed")
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _num(value: float) -> str:
    v = float(value)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)


def _labels(key: Key, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = key + extra
    if not pairs:
        return ""
    body = ",".join(
        '{}="{}"'.format(k, v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
        for k, v in pairs)
    return "{" + body + "}"


class Registry:
    """Gauges, counters and histograms keyed by name + label set."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict[str, tuple[str, str]] = {}          # name -> (kind, help)
        self._values: dict[str, dict[Key, float]] = {}
        self._buckets: dict[str, tuple[float, ...]] = {}
        self._hist: dict[str, dict[Key, list[float]]] = {}   # counts..., sum, count

    # ------------------------------------------------------------ declare
    def gauge(self, name: str, help_text: str) -> None:
        self._declare(name, "gauge", help_text)

    def counter(self, name: str, help_text: str) -> None:
        if not name.endswith("_total"):
            raise ValueError(f"counter {name!r} must end in _total")
        self._declare(name, "counter", help_text)

    def histogram(self, name: str, help_text: str, buckets: tuple[float, ...]) -> None:
        self._declare(name, "histogram", help_text)
        with self._lock:
            self._buckets[name] = tuple(sorted(buckets))
            self._hist.setdefault(name, {})

    def _declare(self, name: str, kind: str, help_text: str) -> None:
        if not _NAME.match(name):
            raise ValueError(f"bad metric name {name!r}")
        with self._lock:
            self._meta[name] = (kind, help_text)
            self._values.setdefault(name, {})

    # -------------------------------------------------------------- write
    def set(self, name: str, value: float | bool | None, /, **labels: Any) -> None:
        """Set a gauge (or a counter whose total the process already keeps).
        `None` is skipped: an absent value is no sample, not a zero."""
        if value is None:
            return
        key = _key(labels)
        with self._lock:
            self._values[name][key] = float(value)

    def inc(self, name: str, amount: float = 1.0, /, **labels: Any) -> None:
        key = _key(labels)
        with self._lock:
            series = self._values[name]
            series[key] = series.get(key, 0.0) + amount

    def observe(self, name: str, value: float, /, **labels: Any) -> None:
        key = _key(labels)
        with self._lock:
            bounds = self._buckets[name]
            row = self._hist[name].setdefault(key, [0.0] * (len(bounds) + 2))
            for i, bound in enumerate(bounds):
                if value <= bound:
                    row[i] += 1
            row[-2] += value
            row[-1] += 1

    def clear(self, name: str) -> None:
        """Drop every series of one family, before re-setting a labelled gauge
        whose label values come and go (platforms, backends)."""
        with self._lock:
            self._values[name] = {}

    # --------------------------------------------------------------- read
    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            for name in sorted(self._meta):
                kind, help_text = self._meta[name]
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {kind}")
                if kind == "histogram":
                    bounds = self._buckets[name]
                    for key in sorted(self._hist[name]):
                        row = self._hist[name][key]
                        for i, bound in enumerate(bounds):
                            out.append(f"{name}_bucket"
                                       f"{_labels(key, (('le', _num(bound)),))} {_num(row[i])}")
                        out.append(f"{name}_bucket{_labels(key, (('le', '+Inf'),))} "
                                   f"{_num(row[-1])}")
                        out.append(f"{name}_sum{_labels(key)} {_num(row[-2])}")
                        out.append(f"{name}_count{_labels(key)} {_num(row[-1])}")
                else:
                    for key in sorted(self._values[name]):
                        out.append(f"{name}{_labels(key)} {_num(self._values[name][key])}")
        return "\n".join(out) + "\n"


REGISTRY = Registry()


# ------------------------------------------------------------ log events
LLM_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0)
RENDER_BUCKETS = (30.0, 60.0, 90.0, 120.0, 180.0, 240.0, 300.0, 420.0, 600.0, 900.0)
RTF_BUCKETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)
STARVE_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
MAX_EVENTS = 9000


def event_name(record: logging.LogRecord) -> str | None:
    """The dotted event a record carries (`program.starved`), read from the
    unformatted message so `"hotstate.tick_failed: %s"` counts as one event.
    `None` when the message is not an event (uvicorn access lines)."""
    msg = record.msg if isinstance(record.msg, str) else ""
    head = msg.split(" ", 1)[0].rstrip(":")
    if len(head) <= 64 and _EVENT.match(head):
        return head
    return None


class LogMetrics(logging.Handler):
    """Counts log events and feeds the histograms from their `extra` fields.

    Reads the record, never the formatted line, and never raises: a metrics
    bug must not break logging. Only the event name, logger name and level
    become labels; `extra` values are used as numbers, plus the three
    config-bounded strings `model`, `status` and `generator`.

    `station_histograms=False` (the web tier) skips the families only the
    origin can produce (`song.produced`, `program.starved`), so the web tier
    exports no empty HELP/TYPE lines for them (MONITORING §2).
    """

    def __init__(self, registry: Registry, *,
                 station_histograms: bool = True) -> None:
        super().__init__(level=logging.INFO)
        self.registry = registry
        self._station = station_histograms
        self._seen: set[str] = set()
        registry.counter("pilgrim_log_events_total",
                         "Log records by logger, dotted event name and level.")
        registry.histogram("pilgrim_llm_call_seconds",
                           "LLM call wall time by model and status.", LLM_BUCKETS)
        if not station_histograms:
            return
        registry.histogram("pilgrim_song_render_seconds",
                           "Song render wall time by generator.", RENDER_BUCKETS)
        registry.histogram("pilgrim_song_render_rtf",
                           "Audio seconds rendered per wall second, by generator.",
                           RTF_BUCKETS)
        registry.histogram("pilgrim_starve_gap_seconds",
                           "Dead-air gap at a program.starved splice.", STARVE_BUCKETS)
        registry.gauge("pilgrim_last_starve_timestamp_seconds",
                       "When the last dead-air gap was logged; 0 when none.")
        registry.set("pilgrim_last_starve_timestamp_seconds", 0)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = event_name(record)
            if event is None:
                if record.levelno < logging.WARNING:
                    return
                event = "other"
            elif event not in self._seen:
                if len(self._seen) >= MAX_EVENTS:
                    event = "other"
                else:
                    self._seen.add(event)
            self.registry.inc("pilgrim_log_events_total", logger=record.name,
                              event=event, level=record.levelname)
            extra = record.__dict__
            if event == "llm.call" and isinstance(extra.get("duration_ms"), (int, float)):
                self.registry.observe(
                    "pilgrim_llm_call_seconds", float(extra["duration_ms"]) / 1000.0,
                    model=str(extra.get("model") or "unknown"),
                    status=str(extra.get("status") or "unknown"))
            elif self._station and event == "song.produced" \
                    and isinstance(extra.get("wall_s"), (int, float)):
                generator = str(extra.get("generator") or "unknown")
                self.registry.observe("pilgrim_song_render_seconds", float(extra["wall_s"]),
                                      generator=generator)
                if isinstance(extra.get("rtf"), (int, float)) and extra["rtf"] > 0:
                    self.registry.observe("pilgrim_song_render_rtf", float(extra["rtf"]),
                                          generator=generator)
            elif self._station and event == "program.starved":
                self.registry.set("pilgrim_last_starve_timestamp_seconds",
                                  record.created)
                if isinstance(extra.get("gap_s"), (int, float)):
                    self.registry.observe("pilgrim_starve_gap_seconds",
                                          float(extra["gap_s"]))
        except Exception:  # noqa: BLE001 - never break logging
            pass


def install_log_metrics(registry: Registry = REGISTRY, *,
                        station_histograms: bool = True) -> LogMetrics:
    """Attach one LogMetrics handler to the root logger (idempotent). Call
    after `setup_logging`, which removes every root handler. The web tier
    passes `station_histograms=False`."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, LogMetrics):
            return h
    handler = LogMetrics(registry, station_histograms=station_histograms)
    root.addHandler(handler)
    return handler


# ----------------------------------------------------------------- server
def serve(registry: Registry, host: str, port: int) -> ThreadingHTTPServer:
    """`GET /metrics` on its own socket, in a daemon thread. Everything else
    is 404. Raises OSError when the port is taken; the caller logs and goes on
    without metrics."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = registry.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server
