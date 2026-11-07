"""The Mac-side Sheep Pen worker (SHEEP_PEN.md §0.5.1, §0.7.7).

`python -m pilgrim.chat_worker` runs three things beside the station, in their
own process and launchd unit: Layer B review (Ornith), Giles dispatches, and
the daily 7-day purge/name-release sweep. It never imports or touches the
scheduler, producer or selector (AGENTS hard rule 1), and every dependency
failure is logged and retried with backoff.

Environment: `REDIS_URL`, `DATABASE_URL` (+ `DB_PASS_SECRET_ARN` or
`DATABASE_PASSWORD`), `LITELLM_TOKEN` — the same repo-root `.env` the station
uses. Audit writes use `media.aws_profile` (`mac-studio`), and a missing
credential is a logged degradation, never a crash.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from pilgrim.chat.actions import ChatActions
from pilgrim.chat.audit import AuditLog
from pilgrim.chat.giles import Giles
from pilgrim.chat.redis_store import ChatStore
from pilgrim.chat.redis_store import connect as chat_connect
from pilgrim.chat.review import Reviewer
from pilgrim.config import Clock, Config, load_api_key, load_config
from pilgrim.db import open_store
from pilgrim.hotstate import REDIS_URL_ENV
from pilgrim.logging_setup import err_text, setup_logging
from pilgrim.pipelines.llm import LLM

log = logging.getLogger("radio.chat.worker")

TICK_S = 30.0
PURGE_INTERVAL_S = 86400.0
HEARTBEAT_TTL_S = 120


def build_worker(cfg: Config) -> dict[str, Any]:
    """Assemble the worker's pieces; raises SystemExit without REDIS_URL."""
    from pilgrim.config import load_secret

    url = load_secret(REDIS_URL_ENV)
    if not url:
        raise SystemExit(f"{REDIS_URL_ENV} is required: the chat worker talks to Valkey")
    store = open_store(cfg)
    cache = chat_connect(url, cfg)
    audit = AuditLog(cfg)
    llm = LLM(cfg, load_api_key())
    from pilgrim.server import _load_prompts

    prompts = _load_prompts(cfg)
    clock = Clock()
    actions = ChatActions(cfg, store, cache, audit, clock)
    return {
        "cfg": cfg, "store": store, "cache": cache, "audit": audit,
        "llm": llm, "clock": clock,
        "reviewer": Reviewer(cfg, store, cache, actions, llm, prompts, clock=clock,
                             audit=audit),
        "giles": Giles(cfg, store, cache, llm, prompts, clock=clock, audit=audit),
    }


async def run(worker: dict[str, Any], stop: asyncio.Event | None = None) -> None:
    cfg: Config = worker["cfg"]
    cache: ChatStore = worker["cache"]
    reviewer: Reviewer = worker["reviewer"]
    giles: Giles = worker["giles"]
    clock: Clock = worker["clock"]
    stop = stop or asyncio.Event()
    wall = clock.wall()
    next_review = wall
    next_giles = wall
    next_purge = wall

    while not stop.is_set():
        wall = clock.wall()
        try:
            await cache.set("worker:heartbeat", str(wall), ex=HEARTBEAT_TTL_S)
        except Exception as e:  # noqa: BLE001 - the heartbeat is a metric
            log.warning("chat.worker.heartbeat_failed", extra={"error": err_text(e)})
        if wall >= next_review:
            try:
                await reviewer.run_once()
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                log.warning("chat.worker.review_failed", extra={"error": err_text(e)})
            next_review = clock.wall() + cfg.chat.review_interval_s
        if wall >= next_giles:
            try:
                await giles.maybe_dispatch()
            except Exception as e:  # noqa: BLE001
                log.warning("chat.worker.giles_failed", extra={"error": err_text(e)})
            next_giles = clock.wall() + cfg.chat.giles_interval_s
        if wall >= next_purge:
            try:
                await reviewer.purge()
            except Exception as e:  # noqa: BLE001
                log.warning("chat.worker.purge_failed", extra={"error": err_text(e)})
            next_purge = clock.wall() + PURGE_INTERVAL_S
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_S)


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _amain() -> None:
    cfg = load_config()
    setup_logging(cfg)
    worker = build_worker(cfg)
    stop = asyncio.Event()
    _install_signals(stop)
    log.info("chat.worker.start", extra={"review_s": cfg.chat.review_interval_s,
                                         "giles_s": cfg.chat.giles_interval_s,
                                         "retention_days": cfg.chat.retention_days})
    try:
        await run(worker, stop)
    finally:
        # Finish the batch in hand before exiting; the cursor is only written
        # after every executor write for that batch committed (SHEEP_PEN.md §B.4).
        await worker["cache"].close()
        worker["store"].close()
        await worker["llm"].close()
        log.info("chat.worker.stop")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
