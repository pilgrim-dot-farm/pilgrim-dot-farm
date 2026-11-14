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
        'pilgrim_inventory_have{type="song"} 0', 'pilgrim_inventory_target{type="song"} 2',
        'pilgrim_rotation{type="song"} 412',
        'pilgrim_backend_up{backend="sfx"} 0', 'pilgrim_backend_up{backend="kokoro"} 1',
        'pilgrim_music_backend_up{name="spx1"} 0', 'pilgrim_music_slots{name="litellm"} 3',
        "pilgrim_search_last_timestamp_seconds 1794000000.5",
        "pilgrim_song_realtime_ratio 0.62",
        'pilgrim_art_backend{state="ok"} 1', 'pilgrim_art_items{with_art="no"} 12',
        'pilgrim_db_engine{engine="postgres"} 1',
        "pilgrim_cache_healthy 1", "pilgrim_cache_ticks_total 38779",
        "pilgrim_cache_failures_total 1",
        "pilgrim_idle_since_timestamp_seconds 1793990000", "pilgrim_idle_wakes_total 4",
        'pilgrim_listeners_by_platform{platform="ios"} 1', "pilgrim_listeners_resumed 3",
    ):
        assert line + "\n" in text, line
    assert "pg-db.example" not in text            # the DB host is not a metric
    assert "pilgrim_search_last_ok" not in text    # backend_up{search} is the health signal


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
    assert 'name="spx1"' not in text and 'pilgrim_art_backend{state="down"} 1' in text


def test_apply_health_tolerates_a_minimal_body():
    reg = _reg()
    sm.apply_health(reg, {"on_air": False, "cache": {"enabled": False}})
    assert "pilgrim_cache_enabled 0\n" in reg.render()


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
    assert "pilgrim_on_air 1\n" in text and "pilgrim_idle 0\n" in text
    assert "pilgrim_listeners 1\n" in text
    assert "pilgrim_committed_coverage_seconds 0\n" not in text
    assert 'pilgrim_current_type{type="' in text
    assert "p-live-0001" not in text
    assert station.idle_wakes == wakes


def test_live_flags_dead_air_when_the_programme_is_dry(tmp_env):
    """MONITORING §2.2: starved is a dry programme, not the idle freeze."""
    cfg, _, _ = tmp_env
    station = build_station(cfg)          # nothing seeded, nothing committed
    reg = _reg()
    sm.live(reg, station)
    text = reg.render()
    assert "pilgrim_on_air 0\n" in text and "pilgrim_starved 1\n" in text
    assert "pilgrim_idle 0\n" in text     # not frozen: dead air, not idle


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
    assert 'pilgrim_reel_ledgers{status="published"} 1\n' in text
    assert 'pilgrim_reel_ledgers{status="failed"} 1\n' in text
    assert 'pilgrim_reel_failed_ledgers{stage="container"} 1\n' in text
    assert "pilgrim_reel_last_posted_timestamp_seconds 1794225600\n" in text
    assert "pilgrim_reel_quota_usage 3\n" in text and "pilgrim_reel_quota_total 100\n" in text
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
