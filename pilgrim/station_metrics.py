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
    g("pilgrim_process_start_timestamp_seconds", "When this process declared its metrics.")
    g("pilgrim_on_air", "1 while the committed programme has coverage.")
    g("pilgrim_idle", "1 while the playhead is frozen (nobody listening).")
    g("pilgrim_idle_since_timestamp_seconds", "When the current freeze began; 0 when awake.")
    c("pilgrim_idle_wakes_total", "Wakes served by this process.")
    g("pilgrim_listeners", "Live players (in-memory map plus the web tier's keys).")
    g("pilgrim_listeners_reporting", "Live players sending playback telemetry.")
    g("pilgrim_listeners_by_platform", "Reporting players by platform.")
    g("pilgrim_listeners_hidden", "Reporting players whose page is hidden or locked.")
    g("pilgrim_listeners_not_running", "Reporting players whose audio context is not running.")
    g("pilgrim_listeners_sink_element", "Reporting players on the element sink.")
    g("pilgrim_listeners_resumed", "Interruption recoveries summed over live players.")
    g("pilgrim_committed_coverage_seconds", "Seconds of committed programme ahead of the playhead.")
    g("pilgrim_committed_items", "Rows in the committed window.")
    g("pilgrim_current_type", "1 for the item type on air now.")
    g("pilgrim_program_revision", "Scheduler revision (bumps on a splice).")
    g("pilgrim_request_stage", "Requests in production, by stage.")
    g("pilgrim_starved", "1 while the programme is dry and the station is not frozen.")
    g("pilgrim_inventory_have", "Inventory the producer counts, by type.")
    g("pilgrim_inventory_target", "Inventory target, by type.")
    g("pilgrim_rotation", "Items that can air now, by type.")
    g("pilgrim_backend_up", "1 when the station's own probe of a backend passes.")
    g("pilgrim_music_backend_up", "1 per healthy song generator.")
    g("pilgrim_music_slots", "Effective render slots per song generator.")
    g("pilgrim_search_last_timestamp_seconds", "When the last real Brave call ran.")
    g("pilgrim_song_realtime_ratio", "Compute seconds per audio second (station estimate).")
    g("pilgrim_art_backend", "1 for the art worker's current backend state.")
    g("pilgrim_art_items", "Songs with and without cover art.")
    g("pilgrim_db_engine", "1 for the store engine this process opened.")
    g("pilgrim_cache_enabled", "1 when the hot-state publisher is configured.")
    g("pilgrim_cache_healthy", "1 when the last hot-state publish succeeded.")
    c("pilgrim_cache_ticks_total", "Hot-state publishes attempted.")
    c("pilgrim_cache_failures_total", "Hot-state publishes failed.")
    g("pilgrim_cache_last_publish_timestamp_seconds", "When the last publish succeeded.")
    g("pilgrim_cache_pending_players", "Player keys waiting for the next publish.")
    g("pilgrim_reel_ledgers", "Reel ledgers on disk, by status.")
    g("pilgrim_reel_failed_ledgers", "Failed Reel ledgers, by the stage they failed at.")
    g("pilgrim_reel_last_posted_timestamp_seconds", "Newest published Reel; 0 when none.")
    g("pilgrim_reel_quota_usage", "Publishing quota used, as of the last publish attempt.")
    g("pilgrim_reel_quota_total", "Publishing quota size, as of the last publish attempt.")
    g("pilgrim_metrics_refresh_timestamp_seconds", "When refresh() last succeeded.")
    g("pilgrim_metrics_refresh_duration_seconds", "How long the last refresh() took.")
    c("pilgrim_metrics_refresh_failures_total", "refresh() or live() raised.")
    reg.set("pilgrim_process_start_timestamp_seconds", time.time())
    reg.inc("pilgrim_metrics_refresh_failures_total", 0)


def live(reg: Registry, station: Any) -> None:
    """In-memory values only: no database, no network (every `live_s`).

    Reads through the station's public surface and never mutates it:
    `listener_count()` is the read-only twin of `active_listeners()` (no
    prune), so a metrics tick cannot influence idle detection (§6 rule 6).
    """
    sched = station.scheduler
    coverage = sched.coverage()
    frozen = station.is_idle()
    reg.set("pilgrim_on_air", coverage > 0)
    reg.set("pilgrim_idle", frozen)
    reg.set("pilgrim_starved", coverage <= 0 and not frozen)
    reg.set("pilgrim_listeners", station.listener_count())
    reg.set("pilgrim_committed_coverage_seconds", round(coverage, 1))
    reg.set("pilgrim_committed_items", len(sched._items))
    reg.set("pilgrim_program_revision", sched.revision)
    reg.clear("pilgrim_current_type")
    seq, _ = sched.on_air()
    row = next((r for r in list(sched._items) if r["seq"] == seq), None)
    if row is not None:
        reg.set("pilgrim_current_type", 1, type=str(row["type"]))
    reg.clear("pilgrim_request_stage")
    stages: dict[str, int] = {}
    for stage in list(station.producer.request_stage.values()):
        stages[str(stage)] = stages.get(str(stage), 0) + 1
    for stage, n in stages.items():
        reg.set("pilgrim_request_stage", n, stage=stage)


def apply_health(reg: Registry, h: dict[str, Any]) -> None:
    """Map the admin health body (RADIO §11) onto gauges. Pure."""
    idle = h.get("idle") or {}
    reg.set("pilgrim_idle_since_timestamp_seconds", idle.get("since") or 0)
    reg.set("pilgrim_idle_wakes_total", idle.get("wakes") or 0)

    lis = h.get("listeners") or {}
    reg.set("pilgrim_listeners_reporting", lis.get("reporting"))
    reg.set("pilgrim_listeners_hidden", lis.get("hidden"))
    reg.set("pilgrim_listeners_not_running", lis.get("not_running"))
    reg.set("pilgrim_listeners_sink_element", lis.get("sink_element"))
    reg.set("pilgrim_listeners_resumed", lis.get("resumed_total"))
    reg.clear("pilgrim_listeners_by_platform")
    for platform, n in (lis.get("by_platform") or {}).items():
        reg.set("pilgrim_listeners_by_platform", n, platform=platform)

    for kind, level in (h.get("inventory") or {}).items():
        reg.set("pilgrim_inventory_have", level.get("have"), type=kind)
        reg.set("pilgrim_inventory_target", level.get("target"), type=kind)
    for kind, n in (h.get("rotation") or {}).items():
        reg.set("pilgrim_rotation", n, type=kind)

    b = h.get("backends") or {}
    for backend in ("litellm", "kokoro", "music", "search", "sfx"):
        reg.set("pilgrim_backend_up", bool(b.get(backend)), backend=backend)
    reg.clear("pilgrim_music_backend_up")
    for name, ok in (b.get("music_backends") or {}).items():
        reg.set("pilgrim_music_backend_up", bool(ok), name=name)
    reg.clear("pilgrim_music_slots")
    for name, slots in (b.get("music_slots") or {}).items():
        reg.set("pilgrim_music_slots", slots, name=name)
    last = b.get("search_last") or {}
    reg.set("pilgrim_search_last_timestamp_seconds", last.get("at") or 0)

    reg.set("pilgrim_song_realtime_ratio", h.get("song_realtime"))
    art = h.get("art") or {}
    reg.clear("pilgrim_art_backend")
    reg.set("pilgrim_art_backend", 1, state=str(art.get("backend") or "disabled"))
    reg.set("pilgrim_art_items", art.get("with_art"), with_art="yes")
    reg.set("pilgrim_art_items", art.get("without_art"), with_art="no")

    reg.clear("pilgrim_db_engine")
    reg.set("pilgrim_db_engine", 1, engine=str((h.get("db") or {}).get("engine") or "unknown"))
    cache = h.get("cache") or {}
    reg.set("pilgrim_cache_enabled", bool(cache.get("enabled")))
    reg.set("pilgrim_cache_healthy", bool(cache.get("healthy")))
    reg.set("pilgrim_cache_ticks_total", cache.get("ticks") or 0)
    reg.set("pilgrim_cache_failures_total", cache.get("failures") or 0)
    reg.set("pilgrim_cache_last_publish_timestamp_seconds", cache.get("last_publish_at") or 0)
    reg.set("pilgrim_cache_pending_players", cache.get("pending_players") or 0)


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
        reg.set("pilgrim_reel_ledgers", n, status=status)
    reg.clear("pilgrim_reel_failed_ledgers")
    for stage, n in reels["failed_at"].items():
        reg.set("pilgrim_reel_failed_ledgers", n, stage=stage)
    reg.set("pilgrim_reel_last_posted_timestamp_seconds", reels["last_posted"])
    quota = reels["quota"] or {}
    reg.set("pilgrim_reel_quota_usage", quota.get("quota_usage"))
    reg.set("pilgrim_reel_quota_total", quota.get("quota_total"))


async def refresh(reg: Registry, station: Any, cfg: Config) -> None:
    t0 = time.monotonic()
    apply_health(reg, await station.health())
    reels = await asyncio.to_thread(read_reels, ROOT / cfg.logging.dir / "reels")
    apply_reels(reg, reels)
    reg.set("pilgrim_metrics_refresh_duration_seconds", round(time.monotonic() - t0, 3))
    reg.set("pilgrim_metrics_refresh_timestamp_seconds", time.time())


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
            reg.inc("pilgrim_metrics_refresh_failures_total")
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
