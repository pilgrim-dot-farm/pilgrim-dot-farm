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
    reg.gauge("pilgrim_on_air", "On air.")
    reg.counter("pilgrim_things_total", "Things.")
    reg.set("pilgrim_on_air", True)
    reg.inc("pilgrim_things_total", kind='a"b\\c')
    reg.inc("pilgrim_things_total", 2, kind='a"b\\c')
    text = reg.render()
    assert "# HELP pilgrim_on_air On air.\n# TYPE pilgrim_on_air gauge\npilgrim_on_air 1\n" in text
    assert "# TYPE pilgrim_things_total counter\n" in text
    assert 'pilgrim_things_total{kind="a\\"b\\\\c"} 3\n' in text
    assert text.endswith("\n")


def test_none_is_no_sample_and_clear_drops_series():
    reg = Registry()
    reg.gauge("pilgrim_by_platform", "x")
    reg.set("pilgrim_by_platform", None, platform="ios")
    assert "pilgrim_by_platform{" not in reg.render()
    reg.set("pilgrim_by_platform", 2, platform="ios")
    reg.clear("pilgrim_by_platform")
    reg.set("pilgrim_by_platform", 1, platform="android")
    text = reg.render()
    assert 'platform="ios"' not in text and 'pilgrim_by_platform{platform="android"} 1' in text


def test_float_and_timestamp_values_keep_precision():
    reg = Registry()
    reg.gauge("pilgrim_ts", "x")
    reg.set("pilgrim_ts", 1794435172.317)
    assert "pilgrim_ts 1794435172.317\n" in reg.render()


def test_histogram_is_cumulative_with_sum_and_count():
    reg = Registry()
    reg.histogram("pilgrim_wait_seconds", "x", (1.0, 5.0))
    for v in (0.5, 2.0, 9.0):
        reg.observe("pilgrim_wait_seconds", v, model="m")
    text = reg.render()
    assert 'pilgrim_wait_seconds_bucket{model="m",le="1"} 1\n' in text
    assert 'pilgrim_wait_seconds_bucket{model="m",le="5"} 2\n' in text
    assert 'pilgrim_wait_seconds_bucket{model="m",le="+Inf"} 3\n' in text
    assert 'pilgrim_wait_seconds_sum{model="m"} 11.5\n' in text
    assert 'pilgrim_wait_seconds_count{model="m"} 3\n' in text


def test_counter_name_and_private_labels_are_refused():
    reg = Registry()
    with pytest.raises(ValueError):
        reg.counter("pilgrim_things", "x")
    reg.gauge("pilgrim_x", "x")
    for label in sorted(FORBIDDEN_LABELS):
        with pytest.raises(ValueError):
            reg.set("pilgrim_x", 1, **{label: "v"})


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
    assert ('pilgrim_log_events_total{event="program.starved",level="WARNING",'
            'logger="radio.scheduler"} 2\n') in text
    assert ('pilgrim_log_events_total{event="other",level="WARNING",'
            'logger="radio.server"} 1\n') in text
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
    assert 'pilgrim_llm_call_seconds_bucket{model="qwen38",status="ok",le="2"} 1\n' in text
    assert 'pilgrim_song_render_seconds_count{generator="audiocpp"} 1\n' in text
    assert 'pilgrim_song_render_rtf_sum{generator="audiocpp"} 1.8\n' in text
    for private in ("secret text", "A Private Title", "abcdef0123456789", "request_id"):
        assert private not in text


def test_starve_records_feed_the_dead_air_gauges():
    """MONITORING §2.2: dead air is a first-class gauge, not only a log rate."""
    reg = Registry()
    h = LogMetrics(reg)
    h.emit(_record("program.starved", logging.WARNING, "radio.scheduler", gap_s=12.5))
    text = reg.render()
    assert "pilgrim_last_starve_timestamp_seconds 0\n" not in text
    assert "pilgrim_last_starve_timestamp_seconds " in text
    assert "pilgrim_starve_gap_seconds_count 1\n" in text
    assert 'pilgrim_starve_gap_seconds_bucket{le="30"} 1\n' in text


def test_web_handler_skips_origin_only_families():
    """MONITORING §2: the web tier exports no empty origin-only HELP/TYPE."""
    reg = Registry()
    LogMetrics(reg, station_histograms=False)
    text = reg.render()
    assert "pilgrim_log_events_total" in text and "pilgrim_llm_call_seconds" in text
    for name in ("pilgrim_song_render_seconds", "pilgrim_song_render_rtf",
                 "pilgrim_starve_gap_seconds", "pilgrim_last_starve_timestamp_seconds"):
        assert name not in text


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
    reg.gauge("pilgrim_on_air", "x")
    reg.set("pilgrim_on_air", 1)
    server = serve(reg, "127.0.0.1", 0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/metrics", timeout=3) as r:
            assert r.status == 200 and r.headers["Content-Type"] == CONTENT_TYPE
            assert b"pilgrim_on_air 1\n" in r.read()
        for path in ("/", "/api/health", "/api/station/program"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path, timeout=3)
            assert e.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
