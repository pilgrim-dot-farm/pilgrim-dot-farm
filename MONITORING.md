# MONITORING.md — application metrics, in-process

Status: plan, rewritten 2026-11-11. Nothing in §4 is built. The earlier
version of this file planned a whole monitoring stack (two Prometheus, yace,
blackbox, host exporters, alert routing). Most of that already exists on the
operator's side or is out of scope here; it is parked, unchanged, in
`docs/backlog/monitoring-infra.md`. **This plan does one thing: make the
application export its own metrics**, from the origin station on the Mac and
from the Fargate web tier, so the existing Prometheus can scrape them.

Read first: `AGENTS.md` (§1 hard rules, §10 "A probe of `/api/station/program`
wakes a frozen station"), RADIO.md §11 (`/api/health` on the admin listener,
"Web tier"), §12, §14 (hot state). Cite this plan as `MONITORING §n`.

---

## 0. Read this first

### 0.1 Facts (verified 2026-11-11)

- Grafana already has a **Pilgrim Dot Farm** folder with two dashboards.
  **Cloud** reads CloudWatch directly: ECS `pg-app` CPU/memory and task counts,
  ElastiCache `pg-cache`, RDS `pg-db`. **Inference** reads Prometheus: LiteLLM
  (`job="litellm"`, `192.168.68.75:4000`), mlx-serve (`job="mlx-serve"`) and,
  since 2026-11-12, the ninfer engines (`job="ninfer"`, `host` = `twr` / `spx0`
  / `spx1`, port 8081; `ninfer_engine_ready` is the "can it generate" signal).
- Prometheus and Grafana run on the operator's **NAS**, on the home LAN. It
  scrapes node exporters on `spx0`, `spx1`, `twr` and `yuri` by IP with a `host`
  label (since 2026-11-12; `.local` names do not resolve inside the Prometheus
  container). It has **no** `pg_*` series.
- The NAS is on the tailnet as `tag:monitoring`. The live tailnet policy grants
  `tag:monitoring -> pilgrim-vpc tcp:9102` (the web tier's metrics port, through
  the bastion's subnet route). The NAS runs `tailscale up --accept-routes`.
- The bastion is SSH alias **`pf2`** (`52.13.70.63`). The alias `pf` in
  `~/.ssh/config` is the retired account's box; do not use it (§8 Q1).
- The station logs one dotted event name per record (`program.starved`,
  `song.produced`, `llm.call`, `request.received`, …) with structured `extra`
  fields. `pilgrim/logging_setup.py` formats them; the names come from string
  constants in the code, so they are a bounded set.
- `Station.health()` (the admin `/api/health` body, RADIO §11) never wakes a
  frozen station. It probes backends with plain `GET`s and runs the same
  inventory counts the producer runs.
- The admin listener (`127.0.0.1:5081`) has unauthenticated control routes. It
  must stay on loopback, so metrics cannot live there if the NAS is to scrape
  them. Metrics get their **own socket**.

### 0.2 Decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | Metrics are **in-process**: the station and the web tier each fill a registry and serve `GET /metrics`. No sidecar exporter, no log tailing | operator, 2026-11-11 |
| D2 | No new dependency. `pilgrim/metrics.py` is a ~200-line registry + text format + stdlib `http.server` | AGENTS §3 |
| D3 | `/metrics` is on **its own port**: `9101` on the Mac, `9102` in the container. Not on the public, admin or ALB port | admin must stay loopback; the ALB target group only knows 8080 |
| D4 | The metrics server and loop start **only** from `server.main()` and `web_app.main()`. `build_station`, `Station.startup`, `create_app`, `create_web_app` are unchanged, so tests, `make sim` and the e2e servers never open the port | AGENTS §1.2 |
| D5 | Log events become counters through a `logging.Handler` on the root logger. **No edit to `scheduler.py`, `producer.py`, `selector.py`, `payments.py` or any pipeline** | AGENTS §1.1, §1.3, §1.11 |
| D6 | Station gauges come from two cadences on the event loop: in-memory values every `metrics.live_s` (5 s), `await station.health()` every `metrics.refresh_s` (30 s). A scrape does no I/O: it renders the registry | playout never waits on monitoring |
| D7 | Metric prefix `pg_`. Web-tier-only families are `pg_web_*`. The Prometheus `job` label (`pg-origin`, `pg-web`) tells the tiers apart for shared families | matches `pg-app`, `pg-db`, `pg-cache` |
| D8 | The web tier is found by Prometheus `aws_sd_configs` `role: ecs`, kept by the service tag `prometheus_scrape=true`, scraped over the bastion's tailnet route | operator, 2026-11-11 |
| D9 | Alerting, probes from outside, and AWS-side failsafes are **not** in this plan | parked in `docs/backlog/monitoring-infra.md` |

### 0.3 Non-goals

- No change to any HTTP route in RADIO §11's table. `/metrics` is a new socket,
  documented in a new §11 subsection.
- No Grafana dashboard JSON in the repo. The operator (or Claude through the
  Grafana MCP) builds the **Station** dashboard after Step 4 ships (§5.6).
- No alert rules. No blackbox or host exporters. No CloudWatch changes.
- No metrics for the chat worker process (`pilgrim/chat_worker.py`); revisit
  after this ships.
- The local agent does **not** apply terraform, edit the live tailnet policy,
  touch the NAS, build images or restart the station. Those are §5, operator.

### 0.4 Files

| File | Step | New / edit |
|------|------|------------|
| `pilgrim/metrics.py` | 1 | new |
| `pilgrim/tests/test_metrics.py` | 1 | new |
| `pilgrim/config.py`, `pilgrim/config.yaml`, `pilgrim/deploy/container/overlay.yaml`, `pilgrim/tests/test_config.py` | 2 | edit |
| `pilgrim/station_metrics.py`, `pilgrim/tests/test_station_metrics.py` | 3 | new |
| `pilgrim/server.py` | 3 | edit (2 lines) |
| `pilgrim/web_metrics.py`, `pilgrim/tests/test_web_metrics.py` | 4 | new |
| `pilgrim/web_app.py` | 4 | edit (2 lines) |
| `RADIO.md`, `AGENTS.md`, `Dockerfile`, `pilgrim/deploy/container/task-definition.json`, `pilgrim/deploy/tailscale/policy.example.hujson`, `docs/README.md` | 5 | edit |

**The code in Steps 1, 3 and 4 was prototyped on 2026-11-11 against commit
`b73cf46`: the three new test files pass (22 tests), `ruff check .` and
`mypy .` pass with the edits in place. The whole unit suite, `make sim` and
`make e2e` were not run on the prototype; the agent runs them per step.**
Copy the code blocks exactly.

---

## 1. Shape

```
 NAS (LAN, tag:monitoring)                     M5 (origin)
 ┌────────────────────────┐   LAN :9101   ┌───────────────────────────────┐
 │ Prometheus             │──────────────▶│ python -m pilgrim.server      │
 │  job pg-origin         │               │  metrics thread: GET /metrics │
 │  job pg-web (ECS SD)   │               │  metrics task: live 5 s,      │
 │ Grafana                │               │   health 30 s  → registry     │
 └───────────┬────────────┘               │  LogMetrics handler → registry│
             │ tailnet, tcp:9102 only     └───────────────────────────────┘
             ▼
 bastion pf2 (subnet router, SNAT) ──▶ Fargate task private IP :9102
                                       python -m pilgrim.web_app
                                        request middleware, cache view,
                                        chat sockets, LogMetrics
```

One registry per process. Writers: the event loop (the metrics task, the web
middleware) and the log handler (any thread). Reader: the metrics server
thread. Every access takes one lock.

---

## 2. Metric catalogue

No label ever carries a player id, voter id, IP, email, token, title, URL,
request id or chat text (§6 rule 3; `metrics.FORBIDDEN_LABELS` enforces it).

### 2.1 Both tiers

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `pg_log_events_total` | counter | `logger`, `event`, `level` | every log record whose message starts with a dotted event name; non-event records only at WARNING and above, as `event="other"` |
| `pg_llm_call_seconds` | histogram | `model`, `status` | `llm.call` records (`duration_ms`) |
| `pg_process_start_timestamp_seconds` | gauge | | set at start |
| `pg_metrics_refresh_timestamp_seconds`, `pg_metrics_refresh_failures_total` | gauge / counter | | the metrics loop about itself |

`pg_log_events_total` is the workhorse. Examples the dashboard will use:
`program.starved`, `program.low_coverage`, `song.produced`, `song.rejected`,
`song.backend.down`, `sfx.failed`, `request.received`, `request.failed`,
`request.aired`, `vote.cast`, `rating.cast`, `payment.applied`,
`payment.refund_due`, `moderation.decision`, `hotstate.down`, `station.wake`,
`station.idle`, `media.published`, `chat.*`, `web.*`.

### 2.2 Origin only (`job="pg-origin"`)

| Metric | Type | Labels | Cadence |
|--------|------|--------|---------|
| `pg_on_air`, `pg_idle`, `pg_listeners` | gauge | | live |
| `pg_committed_coverage_seconds`, `pg_committed_items`, `pg_program_revision` | gauge | | live |
| `pg_current_type` | gauge (1) | `type` | live |
| `pg_request_stage` | gauge | `stage` (`waiting`, `writing`, `generating`, `rendered`) | live |
| `pg_idle_since_timestamp_seconds`, `pg_idle_wakes_total` | gauge / counter | | health |
| `pg_listeners_reporting`, `pg_listeners_hidden`, `pg_listeners_not_running`, `pg_listeners_sink_element`, `pg_listeners_resumed`, `pg_listeners_by_platform` | gauge | `platform` on the last | health |
| `pg_inventory_have`, `pg_inventory_target`, `pg_rotation` | gauge | `type` | health |
| `pg_backend_up` | gauge | `backend` (`litellm`, `kokoro`, `music`, `search`, `sfx`) | health |
| `pg_music_backend_up`, `pg_music_slots` | gauge | `name` | health |
| `pg_search_last_ok`, `pg_search_last_timestamp_seconds` | gauge | | health (Brave is never probed) |
| `pg_song_realtime_ratio` | gauge | | health |
| `pg_art_backend` (1), `pg_art_items` | gauge | `state` / `with_art` | health |
| `pg_db_engine` (1) | gauge | `engine` | health |
| `pg_cache_enabled`, `pg_cache_healthy`, `pg_cache_ticks_total`, `pg_cache_failures_total`, `pg_cache_last_publish_timestamp_seconds`, `pg_cache_pending_players` | gauge / counter | | health |
| `pg_song_render_seconds`, `pg_song_render_rtf` | histogram | `generator` | `song.produced` records that carry `wall_s` |
| `pg_reel_ledgers`, `pg_reel_failed_ledgers`, `pg_reel_last_posted_timestamp_seconds`, `pg_reel_quota_usage`, `pg_reel_quota_total` | gauge | `status` / `stage` | health; read from `pilgrim/logs/reels/*.json`, never the Graph API |
| `pg_metrics_refresh_duration_seconds` | gauge | | health |

### 2.3 Web tier only (`job="pg-web"`)

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `pg_web_http_requests_total` | counter | `route`, `method`, `status` | middleware; `route` is the template (`/api/requests/{request_id}/vote`), `static` for everything outside `/api/` |
| `pg_web_http_request_seconds` | histogram | `route` | middleware |
| `pg_web_cache_up`, `pg_web_station_on_air` | gauge | | one Valkey read of the `health` document every `live_s` |
| `pg_web_chat_sockets` | gauge | | `len(chat_hub.sockets)` |

---

## 3. Before you start

1. `git status` must be clean apart from files you were told about. If
   `pilgrim/server.py`, `pilgrim/web_app.py`, `pilgrim/config.py` or
   `pilgrim/config.yaml` are modified by someone else, stop and report.
2. Do the steps in order. One commit per step. Run the listed checks before
   each commit and paste their last lines into the report (AGENTS §7, §8).
3. Do not start the station. Do not run `make smoke`, `make seed`, or anything
   that reaches a real backend.

---

## 4. Steps

### Step 1 — `pilgrim/metrics.py`

Create `pilgrim/metrics.py` with exactly this content:

```python
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
MAX_EVENTS = 400


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
    """Counts log events and feeds two histograms from their `extra` fields.

    Reads the record, never the formatted line, and never raises: a metrics
    bug must not break logging. Only the event name, logger name and level
    become labels; `extra` values are used as numbers, plus the three
    config-bounded strings `model`, `status` and `generator`.
    """

    def __init__(self, registry: Registry) -> None:
        super().__init__(level=logging.INFO)
        self.registry = registry
        self._seen: set[str] = set()
        registry.counter("pg_log_events_total",
                         "Log records by logger, dotted event name and level.")
        registry.histogram("pg_llm_call_seconds",
                           "LLM call wall time by model and status.", LLM_BUCKETS)
        registry.histogram("pg_song_render_seconds",
                           "Song render wall time by generator.", RENDER_BUCKETS)
        registry.histogram("pg_song_render_rtf",
                           "Audio seconds rendered per wall second, by generator.",
                           RTF_BUCKETS)

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
            self.registry.inc("pg_log_events_total", logger=record.name,
                              event=event, level=record.levelname)
            extra = record.__dict__
            if event == "llm.call" and isinstance(extra.get("duration_ms"), (int, float)):
                self.registry.observe(
                    "pg_llm_call_seconds", float(extra["duration_ms"]) / 1000.0,
                    model=str(extra.get("model") or "unknown"),
                    status=str(extra.get("status") or "unknown"))
            elif event == "song.produced" and isinstance(extra.get("wall_s"), (int, float)):
                generator = str(extra.get("generator") or "unknown")
                self.registry.observe("pg_song_render_seconds", float(extra["wall_s"]),
                                      generator=generator)
                if isinstance(extra.get("rtf"), (int, float)) and extra["rtf"] > 0:
                    self.registry.observe("pg_song_render_rtf", float(extra["rtf"]),
                                          generator=generator)
        except Exception:  # noqa: BLE001 - never break logging
            pass


def install_log_metrics(registry: Registry = REGISTRY) -> LogMetrics:
    """Attach one LogMetrics handler to the root logger (idempotent). Call
    after `setup_logging`, which removes every root handler."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, LogMetrics):
            return h
    handler = LogMetrics(registry)
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
```

Create `pilgrim/tests/test_metrics.py` with exactly this content:

```python
"""pilgrim/metrics.py: registry, text format, log handler, server (MONITORING.md Step 1).

No backend, no station: the server test binds an ephemeral loopback port."""
from __future__ import annotations

import logging
import urllib.error
import urllib.request

import pytest
from pilgrim.metrics import (
    CONTENT_TYPE,
    FORBIDDEN_LABELS,
    LogMetrics,
    Registry,
    event_name,
    install_log_metrics,
    serve,
)


def test_gauge_counter_render_and_escaping():
    reg = Registry()
    reg.gauge("pg_on_air", "On air.")
    reg.counter("pg_things_total", "Things.")
    reg.set("pg_on_air", True)
    reg.inc("pg_things_total", kind='a"b\\c')
    reg.inc("pg_things_total", 2, kind='a"b\\c')
    text = reg.render()
    assert "# HELP pg_on_air On air.\n# TYPE pg_on_air gauge\npg_on_air 1\n" in text
    assert "# TYPE pg_things_total counter\n" in text
    assert 'pg_things_total{kind="a\\"b\\\\c"} 3\n' in text
    assert text.endswith("\n")


def test_none_is_no_sample_and_clear_drops_series():
    reg = Registry()
    reg.gauge("pg_by_platform", "x")
    reg.set("pg_by_platform", None, platform="ios")
    assert "pg_by_platform{" not in reg.render()
    reg.set("pg_by_platform", 2, platform="ios")
    reg.clear("pg_by_platform")
    reg.set("pg_by_platform", 1, platform="android")
    text = reg.render()
    assert 'platform="ios"' not in text and 'pg_by_platform{platform="android"} 1' in text


def test_float_and_timestamp_values_keep_precision():
    reg = Registry()
    reg.gauge("pg_ts", "x")
    reg.set("pg_ts", 1794435172.317)
    assert "pg_ts 1794435172.317\n" in reg.render()


def test_histogram_is_cumulative_with_sum_and_count():
    reg = Registry()
    reg.histogram("pg_wait_seconds", "x", (1.0, 5.0))
    for v in (0.5, 2.0, 9.0):
        reg.observe("pg_wait_seconds", v, model="m")
    text = reg.render()
    assert 'pg_wait_seconds_bucket{model="m",le="1"} 1\n' in text
    assert 'pg_wait_seconds_bucket{model="m",le="5"} 2\n' in text
    assert 'pg_wait_seconds_bucket{model="m",le="+Inf"} 3\n' in text
    assert 'pg_wait_seconds_sum{model="m"} 11.5\n' in text
    assert 'pg_wait_seconds_count{model="m"} 3\n' in text


def test_counter_name_and_private_labels_are_refused():
    reg = Registry()
    with pytest.raises(ValueError):
        reg.counter("pg_things", "x")
    reg.gauge("pg_x", "x")
    for label in sorted(FORBIDDEN_LABELS):
        with pytest.raises(ValueError):
            reg.set("pg_x", 1, **{label: "v"})


def _record(msg, level=logging.INFO, name="radio.test", args=(), **extra):
    rec = logging.LogRecord(name, level, __file__, 1, msg, args, None)
    rec.__dict__.update(extra)
    return rec


def test_event_name_reads_the_unformatted_message():
    assert event_name(_record("program.starved")) == "program.starved"
    assert event_name(_record("hotstate.tick_failed: %s", args=("boom",))) == "hotstate.tick_failed"
    assert event_name(_record('%s - "%s %s HTTP/%s" %d')) is None
    assert event_name(_record("Started server process [%d]")) is None


def test_log_handler_counts_events_and_skips_plain_info():
    reg = Registry()
    h = LogMetrics(reg)
    h.emit(_record("program.starved", logging.WARNING, "radio.scheduler", gap_s=3.0))
    h.emit(_record("program.starved", logging.WARNING, "radio.scheduler", gap_s=1.0))
    h.emit(_record('%s - "%s %s HTTP/%s" %d', logging.INFO, "uvicorn.access"))
    h.emit(_record("LITELLM_TOKEN not set", logging.WARNING, "radio.server"))
    text = reg.render()
    assert ('pg_log_events_total{event="program.starved",level="WARNING",'
            'logger="radio.scheduler"} 2\n') in text
    assert 'pg_log_events_total{event="other",level="WARNING",logger="radio.server"} 1\n' in text
    assert "uvicorn.access" not in text


def test_log_handler_feeds_histograms_and_leaks_no_extra():
    reg = Registry()
    h = LogMetrics(reg)
    h.emit(_record("llm.call", model="qwen38", duration_ms=1500.0, status="ok",
                   error="secret text"))
    h.emit(_record("song.produced", title="A Private Title", wall_s=100.0, rtf=1.8,
                   generator="audiocpp", request_id=7))
    h.emit(_record("song.produced", title="Imported", duration_s=120.0))  # no wall_s
    h.emit(_record("listener.telemetry", player_id="abcdef0123456789", platform="ios"))
    text = reg.render()
    assert 'pg_llm_call_seconds_bucket{model="qwen38",status="ok",le="2"} 1\n' in text
    assert 'pg_song_render_seconds_count{generator="audiocpp"} 1\n' in text
    assert 'pg_song_render_rtf_sum{generator="audiocpp"} 1.8\n' in text
    for private in ("secret text", "A Private Title", "abcdef0123456789", "request_id"):
        assert private not in text


def test_log_handler_never_raises_and_install_is_idempotent():
    reg = Registry()
    h = LogMetrics(reg)
    h.emit(_record(12345))                                  # non-string msg
    h.emit(_record("llm.call", duration_ms="not a number"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        a = install_log_metrics(reg)
        assert install_log_metrics(reg) is a
        assert sum(isinstance(x, LogMetrics) for x in root.handlers) == 1
    finally:
        root.handlers[:] = before


def test_server_serves_metrics_and_404s_everything_else():
    reg = Registry()
    reg.gauge("pg_on_air", "x")
    reg.set("pg_on_air", 1)
    server = serve(reg, "127.0.0.1", 0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/metrics", timeout=3) as r:
            assert r.status == 200 and r.headers["Content-Type"] == CONTENT_TYPE
            assert b"pg_on_air 1\n" in r.read()
        for path in ("/", "/api/health", "/api/station/program"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path, timeout=3)
            assert e.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
```

Checks: `make lint`, `make test`. Commit:
`feat(metrics): in-process registry, text format, log-event handler, /metrics server (RADIO §11; MONITORING Step 1)`

### Step 2 — the `metrics:` config block

**2a. `pilgrim/config.py`.** Insert this class immediately above the line
`class DjCharacter(BaseModel):`

```python
class Metrics(BaseModel):
    """In-process Prometheus metrics (MONITORING.md; RADIO §11 "Metrics", §12).

    `GET /metrics` on its own socket, started only by `server.main()` and
    `web_app.main()`, so tests, the sim and the e2e servers never open it.
    Off by default; `config.yaml` turns it on for the Mac and the container
    overlay moves the port. `live_s` is the in-memory cadence, `refresh_s`
    the admin-health cadence (backend probes, inventory counts)."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 9101
    live_s: float = Field(default=5.0, gt=0)
    refresh_s: float = Field(default=30.0, gt=0)

```

In `class Config(BaseModel):`, directly under the line
`    logging: Logging = Field(default_factory=Logging)` add:

```python
    metrics: Metrics = Field(default_factory=Metrics)
```

**2b. `pilgrim/config.yaml`.** Insert this block immediately above the line
that starts `media:                       # RADIO §8.4`:

```yaml
# In-process Prometheus metrics (MONITORING.md; RADIO §11 "Metrics"). Its own
# socket, GET /metrics only, read-only, no listener ids in any label. The NAS
# Prometheus scrapes it over the LAN, hence 0.0.0.0 (the admin listener stays
# on loopback). Started by `python -m pilgrim.server` only, never by tests.
metrics:
  enabled: true
  host: "0.0.0.0"
  port: 9101
  live_s: 5                # in-memory gauges (coverage, listeners, idle)
  refresh_s: 30            # admin-health gauges (backend probes, inventory)

```

**2c. `pilgrim/deploy/container/overlay.yaml`.** Insert this block immediately
above the line `logging:`:

```yaml
metrics:
  port: 9102               # second socket, never in the ALB target group (terraform ecs_metrics_port)

```

**2d. `pilgrim/tests/test_config.py`.** Append at the end of the file:

```python


def test_metrics_block_defaults_off_and_overlay_moves_the_port():
    """MONITORING Step 2 (RADIO §12)."""
    from pilgrim.config import ROOT, Metrics, load_config
    m = Metrics()
    assert (m.enabled, m.host, m.port) == (False, "127.0.0.1", 9101)
    assert load_config().metrics.port == 9101
    overlay = ROOT / "deploy" / "container" / "overlay.yaml"
    assert load_config(overlay=overlay).metrics.port == 9102
```

Checks: `make lint`, `make test`. Commit:
`feat(config): metrics block — enabled/host/port/live_s/refresh_s; container overlay port 9102 (RADIO §12; MONITORING Step 2)`

(RADIO §12 itself is updated in Step 5, in the same PR/branch; say so in the
report, AGENTS §1.8.)

### Step 3 — the origin station

Create `pilgrim/station_metrics.py` with exactly this content:

```python
"""The origin station's metrics (MONITORING.md §2; RADIO §11 "Metrics").

Two cadences, both on the event loop, both off the playout path (AGENTS §1.1):
`live()` every `metrics.live_s` reads in-memory state only; `refresh()` every
`metrics.refresh_s` awaits `station.health()` (the admin health body: backend
probes and inventory counts, never a wake) and reads the Reel ledgers.

Never imports scheduler, producer, selector or payments (MONITORING §6 rule 2):
the station arrives as an object and is read through its public surface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pilgrim.config import ROOT, Config
from pilgrim.metrics import REGISTRY, Registry, install_log_metrics, serve

log = logging.getLogger("radio.metrics")

_tasks: list[asyncio.Task] = []

REEL_STATUSES = ("building", "built", "captioned", "uploaded", "container",
                 "published", "failed")


def declare(reg: Registry) -> None:
    g, c = reg.gauge, reg.counter
    g("pg_process_start_timestamp_seconds", "When this process declared its metrics.")
    g("pg_on_air", "1 while the committed programme has coverage.")
    g("pg_idle", "1 while the playhead is frozen (nobody listening).")
    g("pg_idle_since_timestamp_seconds", "When the current freeze began; 0 when awake.")
    c("pg_idle_wakes_total", "Wakes served by this process.")
    g("pg_listeners", "Live players (in-memory map plus the web tier's keys).")
    g("pg_listeners_reporting", "Live players sending playback telemetry.")
    g("pg_listeners_by_platform", "Reporting players by platform.")
    g("pg_listeners_hidden", "Reporting players whose page is hidden or locked.")
    g("pg_listeners_not_running", "Reporting players whose audio context is not running.")
    g("pg_listeners_sink_element", "Reporting players on the element sink.")
    g("pg_listeners_resumed", "Interruption recoveries summed over live players.")
    g("pg_committed_coverage_seconds", "Seconds of committed programme ahead of the playhead.")
    g("pg_committed_items", "Rows in the committed window.")
    g("pg_current_type", "1 for the item type on air now.")
    g("pg_program_revision", "Scheduler revision (bumps on a splice).")
    g("pg_request_stage", "Requests in production, by stage.")
    g("pg_inventory_have", "Inventory the producer counts, by type.")
    g("pg_inventory_target", "Inventory target, by type.")
    g("pg_rotation", "Items that can air now, by type.")
    g("pg_backend_up", "1 when the station's own probe of a backend passes.")
    g("pg_music_backend_up", "1 per healthy song generator.")
    g("pg_music_slots", "Effective render slots per song generator.")
    g("pg_search_last_ok", "1 when the last real Brave call did not degrade.")
    g("pg_search_last_timestamp_seconds", "When the last real Brave call ran.")
    g("pg_song_realtime_ratio", "Compute seconds per audio second (station estimate).")
    g("pg_art_backend", "1 for the art worker's current backend state.")
    g("pg_art_items", "Songs with and without cover art.")
    g("pg_db_engine", "1 for the store engine this process opened.")
    g("pg_cache_enabled", "1 when the hot-state publisher is configured.")
    g("pg_cache_healthy", "1 when the last hot-state publish succeeded.")
    c("pg_cache_ticks_total", "Hot-state publishes attempted.")
    c("pg_cache_failures_total", "Hot-state publishes failed.")
    g("pg_cache_last_publish_timestamp_seconds", "When the last publish succeeded.")
    g("pg_cache_pending_players", "Player keys waiting for the next publish.")
    g("pg_reel_ledgers", "Reel ledgers on disk, by status.")
    g("pg_reel_failed_ledgers", "Failed Reel ledgers, by the stage they failed at.")
    g("pg_reel_last_posted_timestamp_seconds", "Newest published Reel; 0 when none.")
    g("pg_reel_quota_usage", "Publishing quota used, as of the last publish attempt.")
    g("pg_reel_quota_total", "Publishing quota size, as of the last publish attempt.")
    g("pg_metrics_refresh_timestamp_seconds", "When refresh() last succeeded.")
    g("pg_metrics_refresh_duration_seconds", "How long the last refresh() took.")
    c("pg_metrics_refresh_failures_total", "refresh() or live() raised.")
    reg.set("pg_process_start_timestamp_seconds", time.time())
    reg.inc("pg_metrics_refresh_failures_total", 0)


def live(reg: Registry, station: Any) -> None:
    """In-memory values only: no database, no network (every `live_s`)."""
    sched = station.scheduler
    reg.set("pg_on_air", station.on_air)
    reg.set("pg_idle", station.is_idle())
    reg.set("pg_listeners", station.active_listeners())
    reg.set("pg_committed_coverage_seconds", round(sched.coverage(), 1))
    reg.set("pg_committed_items", len(sched._items))
    reg.set("pg_program_revision", sched.revision)
    reg.clear("pg_current_type")
    seq, _ = sched.on_air()
    row = next((r for r in list(sched._items) if r["seq"] == seq), None)
    if row is not None:
        reg.set("pg_current_type", 1, type=str(row["type"]))
    reg.clear("pg_request_stage")
    stages: dict[str, int] = {}
    for stage in list(station.producer.request_stage.values()):
        stages[str(stage)] = stages.get(str(stage), 0) + 1
    for stage, n in stages.items():
        reg.set("pg_request_stage", n, stage=stage)


def apply_health(reg: Registry, h: dict[str, Any]) -> None:
    """Map the admin health body (RADIO §11) onto gauges. Pure."""
    idle = h.get("idle") or {}
    reg.set("pg_idle_since_timestamp_seconds", idle.get("since") or 0)
    reg.set("pg_idle_wakes_total", idle.get("wakes") or 0)

    lis = h.get("listeners") or {}
    reg.set("pg_listeners_reporting", lis.get("reporting"))
    reg.set("pg_listeners_hidden", lis.get("hidden"))
    reg.set("pg_listeners_not_running", lis.get("not_running"))
    reg.set("pg_listeners_sink_element", lis.get("sink_element"))
    reg.set("pg_listeners_resumed", lis.get("resumed_total"))
    reg.clear("pg_listeners_by_platform")
    for platform, n in (lis.get("by_platform") or {}).items():
        reg.set("pg_listeners_by_platform", n, platform=platform)

    for kind, level in (h.get("inventory") or {}).items():
        reg.set("pg_inventory_have", level.get("have"), type=kind)
        reg.set("pg_inventory_target", level.get("target"), type=kind)
    for kind, n in (h.get("rotation") or {}).items():
        reg.set("pg_rotation", n, type=kind)

    b = h.get("backends") or {}
    for backend in ("litellm", "kokoro", "music", "search", "sfx"):
        reg.set("pg_backend_up", bool(b.get(backend)), backend=backend)
    reg.clear("pg_music_backend_up")
    for name, ok in (b.get("music_backends") or {}).items():
        reg.set("pg_music_backend_up", bool(ok), name=name)
    reg.clear("pg_music_slots")
    for name, slots in (b.get("music_slots") or {}).items():
        reg.set("pg_music_slots", slots, name=name)
    last = b.get("search_last") or {}
    reg.set("pg_search_last_ok", last.get("status") != "degraded")
    reg.set("pg_search_last_timestamp_seconds", last.get("at") or 0)

    reg.set("pg_song_realtime_ratio", h.get("song_realtime"))
    art = h.get("art") or {}
    reg.clear("pg_art_backend")
    reg.set("pg_art_backend", 1, state=str(art.get("backend") or "disabled"))
    reg.set("pg_art_items", art.get("with_art"), with_art="yes")
    reg.set("pg_art_items", art.get("without_art"), with_art="no")

    reg.clear("pg_db_engine")
    reg.set("pg_db_engine", 1, engine=str((h.get("db") or {}).get("engine") or "unknown"))
    cache = h.get("cache") or {}
    reg.set("pg_cache_enabled", bool(cache.get("enabled")))
    reg.set("pg_cache_healthy", bool(cache.get("healthy")))
    reg.set("pg_cache_ticks_total", cache.get("ticks") or 0)
    reg.set("pg_cache_failures_total", cache.get("failures") or 0)
    reg.set("pg_cache_last_publish_timestamp_seconds", cache.get("last_publish_at") or 0)
    reg.set("pg_cache_pending_players", cache.get("pending_players") or 0)


def read_reels(directory: Path) -> dict[str, Any]:
    """Summarise the Reel ledgers (`pilgrim/logs/reels/*.json`, INSTAGRAM.md).
    Reads that directory only; a broken ledger is skipped."""
    by_status = dict.fromkeys(REEL_STATUSES, 0)
    failed_at: dict[str, int] = {}
    last_posted = 0.0
    quota: tuple[float, dict[str, Any]] | None = None
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text())
            status = str(data.get("status") or "")
            if status not in by_status:
                continue
            by_status[status] += 1
            if status == "failed":
                stage = str(data.get("failed_at") or "unknown")
                failed_at[stage] = failed_at.get(stage, 0) + 1
            if data.get("posted_at"):
                last_posted = max(last_posted,
                                  datetime.fromisoformat(str(data["posted_at"])).timestamp())
            if isinstance(data.get("quota"), dict):
                mtime = path.stat().st_mtime
                if quota is None or mtime > quota[0]:
                    quota = (mtime, data["quota"])
        except (OSError, ValueError, TypeError):
            continue
    return {"by_status": by_status, "failed_at": failed_at, "last_posted": last_posted,
            "quota": quota[1] if quota else None}


def apply_reels(reg: Registry, reels: dict[str, Any]) -> None:
    for status, n in reels["by_status"].items():
        reg.set("pg_reel_ledgers", n, status=status)
    reg.clear("pg_reel_failed_ledgers")
    for stage, n in reels["failed_at"].items():
        reg.set("pg_reel_failed_ledgers", n, stage=stage)
    reg.set("pg_reel_last_posted_timestamp_seconds", reels["last_posted"])
    quota = reels["quota"] or {}
    reg.set("pg_reel_quota_usage", quota.get("quota_usage"))
    reg.set("pg_reel_quota_total", quota.get("quota_total"))


async def refresh(reg: Registry, station: Any, cfg: Config) -> None:
    t0 = time.monotonic()
    apply_health(reg, await station.health())
    reels = await asyncio.to_thread(read_reels, ROOT / cfg.logging.dir / "reels")
    apply_reels(reg, reels)
    reg.set("pg_metrics_refresh_duration_seconds", round(time.monotonic() - t0, 3))
    reg.set("pg_metrics_refresh_timestamp_seconds", time.time())


async def run(reg: Registry, station: Any, cfg: Config) -> None:
    """The metrics loop. A failure is counted and logged, never raised: metrics
    must not take the station down."""
    next_refresh = 0.0
    while True:
        try:
            live(reg, station)
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + cfg.metrics.refresh_s
                await refresh(reg, station, cfg)
        except Exception as e:  # noqa: BLE001
            reg.inc("pg_metrics_refresh_failures_total")
            log.warning("metrics.refresh_failed", extra={"error": f"{type(e).__name__}: {e}"[:300]})
        await asyncio.sleep(cfg.metrics.live_s)


def start(cfg: Config, station: Any, reg: Registry = REGISTRY) -> bool:
    """Called once from `server.main()` inside the running loop, after
    `station.startup()`. False (and a log line) when disabled or the port is
    taken; the station runs either way."""
    if not cfg.metrics.enabled:
        return False
    declare(reg)
    install_log_metrics(reg)
    try:
        serve(reg, cfg.metrics.host, cfg.metrics.port)
    except OSError as e:
        log.warning("metrics.disabled", extra={"error": f"{type(e).__name__}: {e}"[:300]})
        return False
    _tasks.append(asyncio.create_task(run(reg, station, cfg)))
    log.info("metrics.ready", extra={"host": cfg.metrics.host, "port": cfg.metrics.port})
    return True
```

**Edit `pilgrim/server.py`, two lines.**

1. Replace the import line `from pilgrim import voting` with:

```python
from pilgrim import station_metrics, voting
```

2. In `main()`, inside `async def _serve()`, directly under the line
   `        await station.startup()  # exactly once per process` add:

```python
        station_metrics.start(cfg, station)  # MONITORING.md: own socket + task
```

Change nothing else in `server.py`. Do not touch `listener_configs`,
`Station.startup` or `Station.health`.

Create `pilgrim/tests/test_station_metrics.py` with exactly this content:

```python
"""pilgrim/station_metrics.py against a real in-memory Station and a canned
admin-health body (MONITORING.md Step 3). Fakes only: `station.health()` is
never awaited here because it probes backends (AGENTS §1.2)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from conftest import seed_pool
from pilgrim import station_metrics as sm
from pilgrim.metrics import Registry
from pilgrim.server import build_station

HEALTH = {
    "on_air": True,
    "backends": {"litellm": True, "kokoro": True, "music": True, "search": True,
                 "sfx": False, "music_backends": {"litellm": True, "spx1": False},
                 "music_slots": {"litellm": 3},
                 "search_last": {"status": "ok", "at": 1794000000.5, "kind": "news",
                                 "error": ""}},
    "inventory": {"song": {"have": 0, "target": 2}, "news": {"have": 1, "target": 1}},
    "rotation": {"song": 412, "liner": 30},
    "committed_coverage_s": 600.0, "committed_count": 9, "song_realtime": 0.62,
    "art": {"backend": "ok", "with_art": 400, "without_art": 12},
    "db": {"engine": "postgres", "host": "pg-db.example"},
    "cache": {"enabled": True, "healthy": True, "ticks": 38779, "failures": 1,
              "last_error": "", "last_publish_at": 1794000001.0, "pending_players": 0},
    "idle": {"idle": True, "since": 1793990000.0, "wakes": 4},
    "listeners": {"count": 2, "reporting": 2, "by_platform": {"ios": 1, "other": 1},
                  "hidden": 1, "not_running": 0, "sink_element": 1, "resumed_total": 3},
}


def _reg() -> Registry:
    reg = Registry()
    sm.declare(reg)
    return reg


def test_apply_health_maps_the_admin_body():
    reg = _reg()
    sm.apply_health(reg, HEALTH)
    text = reg.render()
    for line in (
        'pg_inventory_have{type="song"} 0', 'pg_inventory_target{type="song"} 2',
        'pg_rotation{type="song"} 412',
        'pg_backend_up{backend="sfx"} 0', 'pg_backend_up{backend="kokoro"} 1',
        'pg_music_backend_up{name="spx1"} 0', 'pg_music_slots{name="litellm"} 3',
        "pg_search_last_ok 1", "pg_search_last_timestamp_seconds 1794000000.5",
        "pg_song_realtime_ratio 0.62",
        'pg_art_backend{state="ok"} 1', 'pg_art_items{with_art="no"} 12',
        'pg_db_engine{engine="postgres"} 1',
        "pg_cache_healthy 1", "pg_cache_ticks_total 38779", "pg_cache_failures_total 1",
        "pg_idle_since_timestamp_seconds 1793990000", "pg_idle_wakes_total 4",
        'pg_listeners_by_platform{platform="ios"} 1', "pg_listeners_resumed 3",
    ):
        assert line + "\n" in text, line
    assert "pg-db.example" not in text            # the DB host is not a metric


def test_apply_health_drops_label_values_that_went_away():
    reg = _reg()
    sm.apply_health(reg, HEALTH)
    later = json.loads(json.dumps(HEALTH))
    later["listeners"]["by_platform"] = {"android": 1}
    later["art"]["backend"] = "down"
    later["backends"]["music_backends"] = {"litellm": True}
    sm.apply_health(reg, later)
    text = reg.render()
    assert 'platform="ios"' not in text and 'state="ok"' not in text
    assert 'name="spx1"' not in text and 'pg_art_backend{state="down"} 1' in text


def test_apply_health_tolerates_a_minimal_body():
    reg = _reg()
    sm.apply_health(reg, {"on_air": False, "cache": {"enabled": False}})
    assert "pg_cache_enabled 0\n" in reg.render()


def test_live_reads_a_real_station_without_waking_it(tmp_env):
    cfg, _, _ = tmp_env
    station = build_station(cfg)
    seed_pool(station.db, cfg)
    station.scheduler.commit_lookahead()
    station.touch_listener("p-live-0001")
    wakes = station.idle_wakes
    reg = _reg()
    sm.live(reg, station)
    text = reg.render()
    assert "pg_on_air 1\n" in text and "pg_idle 0\n" in text
    assert "pg_listeners 1\n" in text
    assert "pg_committed_coverage_seconds 0\n" not in text
    assert 'pg_current_type{type="' in text
    assert "p-live-0001" not in text
    assert station.idle_wakes == wakes


def test_read_reels_summarises_ledgers(tmp_path: Path):
    d = tmp_path / "reels"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({
        "status": "published", "posted_at": "2026-11-09T12:00:00+00:00",
        "quota": {"quota_usage": 3, "quota_total": 100},
        "caption": "private caption", "permalink": "https://example/p/1"}))
    (d / "b.json").write_text(json.dumps({"status": "failed", "failed_at": "container"}))
    (d / "c.json").write_text("{not json")
    reg = _reg()
    sm.apply_reels(reg, sm.read_reels(d))
    text = reg.render()
    assert 'pg_reel_ledgers{status="published"} 1\n' in text
    assert 'pg_reel_ledgers{status="failed"} 1\n' in text
    assert 'pg_reel_failed_ledgers{stage="container"} 1\n' in text
    assert "pg_reel_last_posted_timestamp_seconds 1794225600\n" in text
    assert "pg_reel_quota_usage 3\n" in text and "pg_reel_quota_total 100\n" in text
    assert "private caption" not in text and "example/p" not in text


def test_read_reels_without_a_directory_is_all_zero(tmp_path: Path):
    reels = sm.read_reels(tmp_path / "missing")
    assert set(reels["by_status"].values()) == {0} and reels["last_posted"] == 0.0


def test_start_is_a_no_op_when_disabled(tmp_env):
    cfg, _, _ = tmp_env
    cfg.metrics.enabled = False
    assert sm.start(cfg, object(), Registry()) is False


def test_metrics_modules_never_import_playout_or_payments():
    """MONITORING §6 rule 2."""
    banned = {"pilgrim.scheduler", "pilgrim.producer", "pilgrim.selector",
              "pilgrim.payments", "pilgrim.store", "pilgrim.db"}
    root = Path(sm.__file__).parent
    for name in ("metrics.py", "station_metrics.py", "web_metrics.py"):
        if not (root / name).exists():   # web_metrics.py arrives in Step 4
            continue
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            assert not banned & set(mods), (name, mods)
```

Checks: `make lint`, `make test`, `make sim`. Commit:
`feat(metrics): origin station gauges from live state + admin health + Reel ledgers; started by server.main (RADIO §11; MONITORING Step 3)`

### Step 4 — the web tier

Create `pilgrim/web_metrics.py` with exactly this content:

```python
"""The Fargate web tier's metrics (MONITORING.md §2; RADIO §11 "Metrics").

Request counts and latency per route template, what this task sees in the
cache, and its open chat sockets. Served on `metrics.port`, a second socket
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
    reg.gauge("pg_process_start_timestamp_seconds", "When this process declared its metrics.")
    reg.counter("pg_web_http_requests_total",
                "HTTP requests by route template, method and status.")
    reg.histogram("pg_web_http_request_seconds",
                  "HTTP request wall time by route template.", HTTP_BUCKETS)
    reg.gauge("pg_web_cache_up", "1 when this task can read the hot state from Valkey.")
    reg.gauge("pg_web_station_on_air", "1 when the published health says on air.")
    reg.gauge("pg_web_chat_sockets", "Open chat WebSockets on this task.")
    reg.gauge("pg_metrics_refresh_timestamp_seconds", "When refresh() last succeeded.")
    reg.counter("pg_metrics_refresh_failures_total", "refresh() raised.")
    reg.set("pg_process_start_timestamp_seconds", time.time())
    reg.inc("pg_metrics_refresh_failures_total", 0)


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
            reg.inc("pg_web_http_requests_total", route=route,
                    method=request.method, status=str(status))
            reg.observe("pg_web_http_request_seconds", time.perf_counter() - t0, route=route)


def read_cache(station: Any) -> tuple[bool, bool]:
    """(cache reachable, station on air). One Valkey read, in a worker thread."""
    try:
        doc = station.hot.read("health")
    except Exception:  # noqa: BLE001 - a cache outage is the signal
        return False, False
    return True, isinstance(doc, dict) and bool(doc.get("on_air"))


async def refresh(reg: Registry, station: Any) -> None:
    up, on_air = await asyncio.to_thread(read_cache, station)
    reg.set("pg_web_cache_up", up)
    reg.set("pg_web_station_on_air", on_air)
    hub = getattr(station, "chat_hub", None)
    reg.set("pg_web_chat_sockets", len(hub.sockets) if hub is not None else 0)
    reg.set("pg_metrics_refresh_timestamp_seconds", time.time())


async def run(reg: Registry, station: Any, cfg: Config) -> None:
    while True:
        try:
            await refresh(reg, station)
        except Exception as e:  # noqa: BLE001
            reg.inc("pg_metrics_refresh_failures_total")
            log.warning("metrics.refresh_failed", extra={"error": f"{type(e).__name__}: {e}"[:300]})
        await asyncio.sleep(cfg.metrics.live_s)


def install(cfg: Config, app: FastAPI, station: Any, reg: Registry = REGISTRY) -> bool:
    """Called once from `web_app.main()` before uvicorn starts. Adds the
    request middleware now and starts the server + loop at app startup."""
    if not cfg.metrics.enabled:
        return False
    declare(reg)
    install_log_metrics(reg)
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
```

**Edit `pilgrim/web_app.py`, two lines.**

1. Directly under the line `from pilgrim import dj_contracts as dj` add:

```python
from pilgrim import web_metrics
```

2. In `main()`, directly under the line
   `    app = create_web_app(cfg, station)` add:

```python
    web_metrics.install(cfg, app, station)  # MONITORING.md: own socket, never the ALB's
```

Change nothing else in `web_app.py`. `create_web_app` stays as it is.

Create `pilgrim/tests/test_web_metrics.py` with exactly this content:

```python
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
    assert ('pg_web_http_requests_total{method="GET",route="/api/health",status="200"} 1\n'
            in text)
    assert 'route="/api/requests/{request_id}/vote"' in text
    assert 'route="/api/{rest:path}",status="404"' in text
    assert 'pg_web_http_requests_total{method="GET",route="static",status="200"} 1\n' in text
    assert 'pg_web_http_request_seconds_count{route="/api/health"} 1\n' in text
    assert "987654" not in text and "secret-path" not in text


def test_off_air_is_counted_as_503(tmp_env, monkeypatch):
    _, _, c, reg = _client(tmp_env, monkeypatch, publish=False)
    assert c.get("/api/station/program").status_code == 503
    assert 'route="/api/station/program",status="503"' in reg.render()


def test_refresh_reports_cache_and_station(tmp_env, monkeypatch):
    _, web, _, reg = _client(tmp_env, monkeypatch)
    asyncio.run(wm.refresh(reg, web))
    text = reg.render()
    assert "pg_web_cache_up 1\n" in text and "pg_web_station_on_air 1\n" in text
    assert "pg_web_chat_sockets 0\n" in text

    def boom(name):
        raise ConnectionError("cache down")
    monkeypatch.setattr(web.hot, "read", boom)
    asyncio.run(wm.refresh(reg, web))
    text = reg.render()
    assert "pg_web_cache_up 0\n" in text and "pg_web_station_on_air 0\n" in text


def test_install_is_a_no_op_when_disabled(tmp_env, monkeypatch):
    _, web, _, _ = _client(tmp_env, monkeypatch)
    web.cfg.metrics.enabled = False
    app = create_web_app(web.cfg, web)
    before = len(app.user_middleware)
    assert wm.install(web.cfg, app, web, Registry()) is False
    assert len(app.user_middleware) == before
```

Checks: `make lint`, `make test`, `make sim`, `make e2e`. Commit:
`feat(metrics): web tier request counters, cache view, chat sockets on :9102; installed by web_app.main (RADIO §11; MONITORING Step 4)`

### Step 5 — docs and deploy references

**5a. `RADIO.md` §11.** Find the paragraph heading or sentence that introduces
the web tier (search for `Web tier`). Immediately **before** that subsection,
add:

```markdown
### Metrics (MONITORING.md)

Both processes export Prometheus metrics in-process, on a socket of their own:
`GET /metrics` on `metrics.host:metrics.port` (§12; `9101` on the Mac, `9102`
in the container). That socket answers nothing else (404), is not the public,
admin or ALB port, and is opened only by `python -m pilgrim.server` and
`python -m pilgrim.web_app`, never by tests, the sim or the e2e servers.

A scrape does no I/O: it renders a registry. The station fills it from a task
on the event loop: in-memory state every `metrics.live_s`, and the admin
health body (`Station.health()`, which never wakes a frozen station) every
`metrics.refresh_s`. The web tier fills it from a request middleware (route
template, method, status, latency), one Valkey read of the `health` document
per `live_s`, and its open chat sockets. Both attach a logging handler that
counts every dotted log event as `pg_log_events_total{logger,event,level}`.
The catalogue is MONITORING.md §2. No label carries a player id, voter id, IP,
title, URL, request id or chat text; the registry refuses those label names.
The metrics modules never import `scheduler.py`, `producer.py`, `selector.py`,
`payments.py`, `store.py` or `db.py`, and a failure in them is logged
(`metrics.refresh_failed`, `metrics.disabled`), never raised.
```

**5b. `RADIO.md` §12.** In the YAML sketch, immediately above the line
`logging:` add:

```yaml
metrics:                   # MONITORING.md; §11 "Metrics". GET /metrics on its own socket
  enabled: true            # model default false; config.yaml turns it on
  host: "0.0.0.0"          # LAN-reachable for the NAS Prometheus; admin stays loopback
  port: 9101               # container overlay: 9102 (never in the ALB target group)
  live_s: 5                # in-memory gauges
  refresh_s: 30            # admin-health gauges (backend probes, inventory counts)

```

**5c. `AGENTS.md`.**

1. In the plan list at the top, add after the `sheep-pen-plan.md` bullet:

```markdown
- `MONITORING.md` — in-process Prometheus metrics for the station (`:9101`)
  and the web tier (`:9102`); Steps 1–5, then an operator handoff. The parked
  infrastructure plan is `docs/backlog/monitoring-infra.md`.
```

2. In the §2 Environment table, add a row after the `LiteLLM relay` row:

```markdown
| Prometheus + Grafana | operator's NAS (LAN; tailnet `tag:monitoring`) | scrapes `GET /metrics` on the M5 `:9101` (LAN) and on each Fargate task `:9102` (bastion route, ECS discovery). Read-only; the station never calls it (MONITORING.md) |
```

3. In §10 Known pitfalls, add at the end:

```markdown
- **`/metrics` has its own port and only `main()` opens it** (MONITORING.md):
  `9101` on the Mac, `9102` in the container. Never mount it on the public or
  admin app, never start it from `build_station`/`create_web_app`, and never
  add a label that could hold a listener id or request text.
```

**5d. `Dockerfile`.** Replace the line `EXPOSE 8080` with:

```dockerfile
EXPOSE 8080 9102
```

**5e. `pilgrim/deploy/container/task-definition.json`.** Replace

```json
      "portMappings": [
        {
          "containerPort": 8080,
          "protocol": "tcp",
          "appProtocol": "http"
        }
      ],
```

with

```json
      "portMappings": [
        {
          "containerPort": 8080,
          "protocol": "tcp",
          "appProtocol": "http"
        },
        {
          "name": "metrics",
          "containerPort": 9102,
          "protocol": "tcp"
        }
      ],
```

**5f. `pilgrim/deploy/tailscale/policy.example.hujson`** (mirrors what the
operator already applied on 2026-11-11). Replace

```hujson
    "tag:radio-relay": ["autogroup:admin"],
  },
```

with

```hujson
    "tag:radio-relay": ["autogroup:admin"],
    "tag:monitoring":  ["autogroup:admin"], // the NAS (Prometheus + Grafana), MONITORING.md
  },
```

and replace the line `    // No edge-to-edge grant. No SSH rules.` with:

```hujson
    // NAS Prometheus -> web tier /metrics inside the VPC, through the
    // bastion's subnet route (MONITORING.md). Only this port.
    {"src": ["tag:monitoring"], "dst": ["pilgrim-vpc"], "ip": ["tcp:9102"]},
    // No edge-to-edge grant. No SSH rules.
```

**5g. `docs/README.md`.** Add one index line for `MONITORING.md` (application
metrics plan) and one for `docs/backlog/monitoring-infra.md` (parked
infrastructure and alerting plan), in the style of the neighbouring lines.

Checks: `make lint`, `make test`, `make deploy-check`. Commit:
`docs: metrics — RADIO §11 "Metrics", §12 metrics block, AGENTS env + pitfall, container port 9102, tailnet tag:monitoring grant (MONITORING Step 5)`

Then stop and write the AGENTS §8 report. **Do not deploy.**

---

## 5. Operator handoff (not the agent)

### 5.1 Terraform, `~/projects/terraform-aws-pilgrim/modules/ec2`

`variables.tf`:

```hcl
variable "ecs_metrics_port" {
  description = "Prometheus /metrics listener in the web container. Never registered with the ALB."
  type        = number
  default     = 9102
}
```

`security_groups.tf` (the bastion's subnet router SNATs, and the bastion and
the tasks share the `pg` group, so the rule references itself):

```hcl
# Prometheus scrape of the web tier, through the bastion's subnet route (MONITORING.md).
resource "aws_vpc_security_group_ingress_rule" "metrics" {
  security_group_id            = aws_security_group.pg.id
  referenced_security_group_id = aws_security_group.pg.id
  from_port                    = var.ecs_metrics_port
  to_port                      = var.ecs_metrics_port
  ip_protocol                  = "tcp"
}
```

`ecs.tf`, in the `app` service next to `name`:

```hcl
      service_tags = { prometheus_scrape = "true" }
```

and the container's `portMappings` (leave `load_balancer` alone):

```hcl
          portMappings = [
            { name = "http", containerPort = var.ecs_container_port, protocol = "tcp" },
            { name = "metrics", containerPort = var.ecs_metrics_port, protocol = "tcp" },
          ]
```

`iam.tf`:

```hcl
data "aws_iam_policy_document" "ecs_discovery" {
  statement {
    actions = [
      "ecs:ListClusters", "ecs:DescribeClusters",
      "ecs:ListServices", "ecs:DescribeServices",
      "ecs:ListTasks", "ecs:DescribeTasks",
      "ec2:DescribeNetworkInterfaces",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "ecs_discovery" {
  name   = "ecs-discovery-read"
  user   = aws_iam_user.logs.name
  policy = data.aws_iam_policy_document.ecs_discovery.json
}
```

`terraform plan` should show: one ingress rule, one user policy, the service
tag, and a new task definition revision (rolling deploy).

### 5.2 Ship the code

1. Web tier: `make image`, push `pilgrimdotfarm/front-end`, bump `ecs_release`,
   `terraform apply`.
2. Origin: restart the station on the Mac (`farm.pilgrim.station`). Look for
   `metrics.ready host=0.0.0.0 port=9101` in the station log.

### 5.3 Verify, before touching Prometheus

```sh
# on the Mac
curl -s 127.0.0.1:9101/metrics | grep -c '^pg_'            # > 50
curl -s 127.0.0.1:9101/metrics | grep '^pg_on_air'
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:9101/api/health   # 404

# from the bastion (inside the VPC); TASK_IP from the ECS console
ssh pf2 "curl -s http://TASK_IP:9102/metrics | grep '^pg_web_'"

# the ALB must NOT serve it
curl -s -o /dev/null -w '%{http_code}\n' https://pilgrim.farm/metrics  # 404 or the site's fallback, never metrics text

# from the NAS (tailnet route + grant + security group)
curl -s http://TASK_IP:9102/metrics | head -3
```

If Prometheus runs in a container on the NAS, run the last check from inside
that container.

### 5.4 Prometheus on the NAS

`kenny-logger`'s key goes in the Prometheus user's `~/.aws/credentials` as a
profile (or in its environment). Then:

```yaml
  - job_name: pg-origin
    static_configs:
      - targets: ["<M5 LAN address>:9101"]

  - job_name: pg-web
    aws_sd_configs:
      - role: ecs
        region: us-west-2
        profile: kenny-logger
        clusters: ["arn:aws:ecs:us-west-2:477688903218:cluster/pg-ecs"]
        port: 9102
    relabel_configs:
      - source_labels: [__meta_ecs_tag_service_prometheus_scrape]
        regex: "true"
        action: keep
      - source_labels: [__meta_ecs_last_status]
        regex: RUNNING
        action: keep
      - source_labels: [__meta_ecs_task_arn]
        regex: ".*/([^/]+)$"
        target_label: task
```

`role: ecs` is a recent addition to Prometheus; check `prometheus --version`
against the changelog first (§8 Q4). If the NAS's build predates it, upgrade,
or fall back to a `file_sd_configs` file refreshed by a cron script that calls
`ecs:ListTasks`/`DescribeTasks`.

Done when `up{job="pg-origin"} == 1` and `up{job="pg-web"} == 1`.

### 5.5 What scraping costs the station

Nothing on the playout path. The 30 s refresh makes the same backend `GET`s
and inventory counts the admin `/api/health` makes on demand. If that ever
shows up as load, raise `metrics.refresh_s`; the live gauges are unaffected.

### 5.6 Station dashboard

A third dashboard in the **Pilgrim Dot Farm** folder, built once real series
exist. Suggested rows: on air / idle / listeners / coverage; inventory vs
target and rotation; backends and music slots; requests (stage gauge, event
rates); renders (`pg_song_render_seconds`, `pg_song_render_rtf`, LLM latency);
hot-state publisher; web tier (requests by route and status, p95 latency,
cache view, chat sockets); log events by level; Reels.

---

## 6. Hard rules for this plan

In addition to AGENTS §1:

1. Metrics code never calls `GET /api/station/program` or `POST
   /api/station/heartbeat`, never probes Brave, FLUX or the Meta Graph API,
   never asks a model for anything. The only station call is
   `Station.health()`, in-process.
2. `pilgrim/metrics.py`, `pilgrim/station_metrics.py` and
   `pilgrim/web_metrics.py` never import `scheduler`, `producer`, `selector`,
   `payments`, `store` or `db` (a test enforces it). They read the station
   object they are handed.
3. No label carries a player id, voter id, user id, IP, email, token, title,
   URL, request id, item id or text. Label values are config-bounded names
   only (types, backends, models, generators, route templates, event names).
4. `/metrics` is never mounted on the public, admin or ALB-facing app, and is
   never reachable through Cloudflare.
5. A metrics failure is logged and counted. It never raises into the station
   or the web app, and a taken port means "no metrics", not "no station".
6. Scrape handlers do no I/O beyond rendering the registry.

---

## 7. Acceptance checklist

- [ ] `make lint`, `make test`, `make sim`, `make e2e` pass after Step 4.
- [ ] `make test` never binds `9101` or `9102` (it passes while the live
      station is running on this Mac).
- [ ] `git diff` for `scheduler.py`, `producer.py`, `selector.py`,
      `payments.py` and `pilgrim/pipelines/` is empty.
- [ ] `server.py` and `web_app.py` each changed by exactly two lines.
- [ ] RADIO §11 has "Metrics"; RADIO §12 has the `metrics:` block.
- [ ] (operator) §5.3 checks pass; both `up` series are 1; the Station
      dashboard exists.

---

## 8. Open questions (operator)

1. **`pf` vs `pf2`.** `~/.ssh/config` has `pf` → `52.53.93.0` (retired account)
   and `pf2` → `52.13.70.63` (the bastion). This plan says `pf2`. Repoint `pf`
   if you want that name to mean the bastion.
2. **The M5's LAN address.** The parked plan says `192.168.68.51`; Prometheus
   scrapes mlx-serve at `192.168.68.90`. Whichever is the Mac goes in the
   `pg-origin` target.
3. **`metrics.host: "0.0.0.0"` on the Mac.** It exposes `:9101` (read-only, no
   private labels) to the home LAN, like mlx-serve's `:11234`. The tailnet
   policy has no grant for it. Say so if you would rather bind the LAN address.
4. **Prometheus version on the NAS** for `aws_sd_configs` `role: ecs` (§5.4).
5. **Alerting.** Out of scope here. Once the series exist, the catalogue in
   `docs/backlog/monitoring-infra.md` §4 can be cut down to the rows that use
   `pg_*` (#4, #7, #10–12, #15–17, #24–25, #31–33) as Grafana-managed alerts.

---

## 9. Parked

`docs/backlog/monitoring-infra.md` is the previous version of this file,
verbatim under a short header: the two-Prometheus design, yace, blackbox
probes, host and GPU exporters, CloudWatch failsafe alarms, the Synthetics
canary, budgets, the alert catalogue and the agent responder. Its §1 survey
(2026-11-07) is still the best host inventory. Nothing there is scheduled.
