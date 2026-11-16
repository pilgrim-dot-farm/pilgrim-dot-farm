"""Songs: the job decider, stock and request production, intros (RADIO §6.1, §6.2).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any

import httpx

from pilgrim import genres
from pilgrim.logging_setup import err_text
from pilgrim.pipelines.llm import LLMTruncated
from pilgrim.pipelines.request_parts import from_row
from pilgrim.producer.gates import (
    BRIEFS_AHEAD,
    LLM_FREE_CUTS,
    SLOW_CHECK_S,
    STOCK_BRIEF_KEY,
    STOCK_RENDER_KEY,
    SongJob,
    _bench_opts,
    _no_gate,
)
from pilgrim.producer.promos import PromoProducer

log = logging.getLogger("radio.producer")


class SongProducer(PromoProducer):
    # ----------------------------------------------------- retained intros
    def _requests_in_flight(self) -> bool:
        """A listener request is being produced or is waiting: the backends
        are busy with work somebody asked for."""
        if any(j.kind == "request" for j in self._jobs.values()):
            return True
        return bool(self.store.queued_requests(
            bury_threshold=self.cfg.voting.bury_threshold))

    async def ensure_song_intros(self) -> None:
        """Give songs that have no reusable intro a code-written one ("Here's
        {title} from {artist}"), kept and replayed with the song (RADIO §5.2
        "Break structure"). Kokoro only: no LLM, no song model. A few per
        pass, and none while a listener request is in flight, so this never
        competes with the queue; a song without one simply airs without it."""
        per_pass = self.cfg.inventory.intro_backfill_per_pass
        if per_pass <= 0 or self.clock.now() < self._intro_check_after \
                or self._requests_in_flight():
            return
        have: set[int] = set()
        for it in self.store.list_items("intro"):
            if it["retired"] or it["emergency"]:
                continue
            try:
                meta = json.loads(it["meta_json"]) if it.get("meta_json") else {}
            except ValueError:
                continue
            if meta.get("request_id") is None and meta.get("song_item_id") is not None:
                have.add(int(meta["song_item_id"]))
        made = 0
        for song in self.store.list_items("song"):
            if made >= per_pass:
                break
            if song["retired"] or song["emergency"] or song["id"] in have \
                    or song["id"] in self._intro_rejected or not song.get("title"):
                continue
            intro = await self._render_intro(song, None)
            if intro is None:
                # the same title would fail the same way: do not retry it
                # every pass (see ensure_time_checks)
                self._intro_rejected.add(int(song["id"]))
                continue
            self._store_intro(intro, int(song["id"]), None)
            made += 1
            log.info("intro.backfilled", extra={
                "song_item_id": song["id"], "title": song.get("title")})
            if self._requests_in_flight():
                break  # a request arrived: get out of its way
        if made == 0:
            # every song has one (or cannot have one): reading the whole song
            # and intro tables every pass would be waste; look again later
            self._intro_check_after = self.clock.now() + SLOW_CHECK_S

    async def song_loop(self) -> None:
        """Song decider (RADIO §6.1/§6.2). Keeps up to `songs.capacity()` song
        **renders** in flight, plus one request brief written ahead of them,
        each job one brief -> render -> intro -> store:
        - the next listener request first (queue order: paid, then FIFO). Up to
          songs.free_request_slots free requests render at once, paid ones in any
          free slot; paid jobs are bounded at `capacity() + BRIEFS_AHEAD`, so a
          queue of paid requests cannot pile up unbounded;
        - a job that has its brief waits for a generator slot at the render
          gate, which grants by queue order (a paid request is never behind
          free work);
        - a finished request waits (`_wait_turn`) until those ahead of it are
          ready, because ready order is air order;
        - free requests and stock never take the last slot, which stays open
          for a paid request (with capacity 1 they use it when it is idle);
        - a backend outage (transport error) pauses new jobs 15 s doubling to
          2 min; any other failure pauses 15 s (a dead LLM must not spin briefs).
        Jobs are never cancelled for a request: a generator doesn't abort on
        disconnect, so cancelling only wastes the render."""
        log.info("song worker started")
        outage_s = 15.0
        try:
            while True:
                pause = 0.0
                for task in [t for t in self._jobs if t.done()]:
                    outcome = self._reap(task, self._jobs.pop(task))
                    if outcome == "outage":
                        pause = max(pause, outage_s)
                        outage_s = min(outage_s * 2, 120.0)
                    elif outcome == "failed":
                        pause = max(pause, 15.0)
                    else:
                        outage_s = 15.0
                if pause:
                    await self._sleep(pause)
                    continue
                # a backend back from cooldown may unblock jobs waiting here
                self._render_gate.pump()
                while (job := self._next_job()) is not None:
                    self._spawn(job)
                tick = asyncio.ensure_future(self._sleep(20.0))
                freed = asyncio.ensure_future(self._render_done.wait())
                try:
                    await asyncio.wait({tick, freed, *self._jobs},
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    tick.cancel()
                    freed.cancel()
                    self._render_done.clear()
        finally:
            for task in self._jobs:
                task.cancel()

    def _spawn(self, job: SongJob) -> None:
        if job.req is not None:
            # claim it now: the next _next_job() runs before the task does.
            # claimed_at (docs/shipped/voting.md V6) orders in-flight work by claim time;
            # it is set once and kept across retries.
            job.req["claimed_at"] = self.store.mark_request_producing(job.req["id"])
            self.request_stage[job.req["id"]] = "waiting"
        task = asyncio.create_task(self._run_job(job))
        self._jobs[task] = job
        log.info("song.job.spawned", extra={
            "kind": job.kind, "paid": job.paid,
            "request_id": job.req["id"] if job.req else None,
            "jobs": len(self._jobs), "capacity": self._capacity()})

    def _reap(self, task: asyncio.Task, job: SongJob) -> str:
        """Observe a finished job (no task dies unobserved): ok | outage | failed."""
        rid = job.req["id"] if job.req else None
        if task.cancelled():
            return "failed"
        exc = task.exception()
        if exc is None:
            outcome = "ok"
        elif isinstance(exc, (httpx.TransportError, LLMTruncated)):
            outcome = "outage"
            log.warning("song.backend_unavailable: %s", err_text(exc))
        else:
            outcome = "failed"
            log.warning("song.production_failed: %s", err_text(exc))
        log.info("song.job.reaped", extra={
            "kind": job.kind, "request_id": rid, "outcome": outcome,
            "jobs": len(self._jobs)})
        return outcome

    def _next_job(self) -> SongJob | None:
        """Pick the next song job given what is in flight (sync, no awaits).
        A job's expensive part is the render, so a request brief (qwen38) is
        written while another job renders: paid jobs are bounded at
        `capacity() + BRIEFS_AHEAD`, free work by the reserved-slot rule and
        `songs.free_request_slots` (§6.1). A job that has its brief waits for a
        slot at the render gate, granted by queue order.

        Same gates as ever for the serial path (`song_step`, nothing in flight):
        a queued request first, even at stock target or library cap; else stock
        if fresh (+ in-flight stock) is below target, the library is under
        songs.stock_max_library and songs.stock_gap_s has passed (§6.1)."""
        every = list(self._jobs.values())
        # a job whose render has returned (intro, waiting its turn to air)
        # holds no slot: it must not keep the next request out of one
        jobs = [j for j in every if j.phase != "done"]
        cap = self._capacity()
        if cap <= 0:
            return None
        free_jobs = sum(1 for j in jobs if not j.paid)
        paid_jobs = len(jobs) - free_jobs
        # free requests and stock together never fill every slot: one stays
        # open for a paid request. Paid jobs don't count against that, so with
        # two paid rendering a free one may take the third. With a single slot
        # there is nothing to reserve: the render gate keeps a paid request
        # ahead of free work that already has its brief (plan §2.3).
        free_total, free_requests = self._free_render_caps()
        # first eligible queued request in queue order: paid may take any slot
        # (several at once); free only a non-last slot, up to
        # songs.free_request_slots. Finished songs still air in queue order
        # (_wait_turn), so starting a later request early is safe.
        free_busy = sum(1 for j in jobs if j.kind == "request" and not j.paid)
        # one free request past those limits is briefed ahead of its slot and
        # waits at the render gate, which enforces the same two limits
        free_ok = (free_busy < free_requests + BRIEFS_AHEAD
                   and free_jobs < free_total + BRIEFS_AHEAD)
        # that request holds no slot, so it doesn't count against stock
        ahead = max(0, free_busy - free_requests, free_jobs - free_total)
        slot_jobs = free_jobs - ahead
        claimed = {j.req["id"] for j in every if j.req is not None}
        for req in self.store.queued_requests(
                bury_threshold=self.cfg.voting.bury_threshold):
            if req["id"] in claimed:
                continue
            # a paid request is briefed ahead of the slot it waits for, but
            # only one ahead: a queue of paid requests is bounded like any
            # other work (RADIO §6.1)
            if req.get("priority"):
                if paid_jobs < cap + BRIEFS_AHEAD:
                    return SongJob("request", req)
                continue
            if free_ok:
                return SongJob("request", req)
        # stock only starts when a slot is free: a listener request never waits
        # for a render behind stock that already has its words (plan §2.3)
        if slot_jobs < free_total and self._stock_due(
                sum(1 for j in every if j.kind == "stock")):
            return SongJob("stock")
        return None

    def _stock_due(self, in_flight: int) -> bool:
        if self.idle():
            # docs/shipped/idle.md D6: stock songs serve airtime nobody hears. Queued
            # requests are still picked above this check.
            if not self._idle_stock_logged:
                log.info("song.stock_paused_idle")
                self._idle_stock_logged = True
            return False
        self._idle_stock_logged = False
        if self.stock_capped():
            if not self._stock_cap_logged:
                log.info("song.stock_capped", extra={
                    "library_songs": self.store.count_usable_of_type("song"),
                    "stock_max_library": self.cfg.songs.stock_max_library})
                self._stock_cap_logged = True
            return False
        self._stock_cap_logged = False
        # in-flight stock counts toward the target, or cold start overshoots
        fresh = self.store.count_fresh_of_type("song") + in_flight
        if fresh >= self.cfg.inventory.fresh_songs_ready:
            return False
        return self._last_stock_song_at is None or (
            self.clock.now() - self._last_stock_song_at >= self.cfg.songs.stock_gap_s)

    async def _run_job(self, job: SongJob) -> bool:
        try:
            if job.req is not None:
                return await self._produce_request_song(job.req, job)
            return await self._produce_stock_song(job)
        finally:
            if job.req is not None:
                self.request_stage.pop(job.req["id"], None)
            job.finished.set()

    async def _wait_turn(self, job: SongJob | None) -> None:
        """Hold a finished request song until every request AHEAD of it in
        queue order that is still in flight has ended. Ready order is air
        order (the splice, §5.2), so parallel requests must become ready in
        queue order. The song waits outside the library: stored now, the
        scheduler could air it as stock. If the one ahead fails or is
        re-queued, this one goes."""
        if job is None:
            return
        while True:
            ahead = [j for j in self._jobs.values()
                     if j is not job and j.req is not None and not j.finished.is_set()
                     and j.queue_key() < job.queue_key()]
            if not ahead:
                return
            log.info("request.held", extra={
                "request_id": job.req["id"] if job.req else None,
                "behind": [j.req["id"] for j in ahead if j.req]})
            waits = {asyncio.ensure_future(j.finished.wait()) for j in ahead}
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()

    async def song_step(self) -> bool:
        """One song job, run to completion inline (seed, tests, the sim).
        Returns True if it did work. Gates: `_next_job`."""
        job = self._next_job()
        if job is None:
            return False
        return await self._run_job(job)

    def _genre_context(self) -> list[str]:
        """`_recent_genres()` plus the genres of in-flight jobs whose brief is
        written: a parallel render isn't in the store yet, but it will air
        next to this one (§5.2 genre_no_repeat)."""
        n = self.cfg.playout.genre_no_repeat
        out = self._recent_genres()
        for j in self._jobs.values():
            if j.genre:
                if j.genre in out:
                    out.remove(j.genre)
                out.append(j.genre)
        return out[-n:]

    def _start_early_art(self, brief: dict[str, Any], token: str,
                         request_text: str | None = None,
                         paid: bool = False,
                         parts=None):
        """Start the cover render now that the brief (title/artist/lyrics/
        style) exists, in parallel with the song generator. Optional."""
        aw = self.art_worker
        if aw is None or not self.cfg.art.enabled:
            return None
        if not (brief.get("title") and brief.get("artist")):
            return None
        # share the song seed so the art meta records a reproducible seed
        brief.setdefault("seed", secrets.randbits(32))
        return asyncio.create_task(aw.render_brief(
            brief, token, request_text=request_text, paid=paid, parts=parts))

    def _finish_early_art(self, song_id: int, task) -> None:
        """Attach an early cover to the item, or let the background worker
        pick it up if the render failed/was slower than the song."""
        aw = self.art_worker
        if task is None or aw is None:
            return
        aw.pending_early.add(song_id)
        if task.done():
            self._attach_early_art(song_id, task, aw)
        else:
            task.add_done_callback(
                lambda t, sid=song_id, worker=aw:
                self._attach_early_art(sid, t, worker))

    def _attach_early_art(self, song_id: int, task, aw) -> None:
        if task.cancelled():
            aw.clear_pending(song_id)
            return
        try:
            result = task.result()
        except Exception:  # noqa: BLE001 - early art is cosmetic
            result = None
        if result is None:
            aw.clear_pending(song_id)
            return
        jpg_path, meta = result
        try:
            aw.attach(song_id, jpg_path, meta)
        except Exception as e:  # noqa: BLE001 - DB hiccup: worker retries later
            log.warning("art.attach_failed", extra={
                "item_id": song_id, "stage": "attach", "error": err_text(e)})
            aw.clear_pending(song_id)
            return
        log.info("art.attached", extra={
            "item_id": song_id, "stage": "attach", "early": True})

    async def _produce_stock_song(self, job: SongJob) -> bool:
        art_task = None
        attached = False
        try:
            async with self._brief_gate.hold(STOCK_BRIEF_KEY):
                brief = await self.songs.brief(self._genre_context())
                job.genre = brief.get("genre")
            art_task = self._start_early_art(
                brief, f"stock{secrets.token_hex(6)}")
            # the words are written: now wait for a generator slot (queue order)
            job.phase = "render"
            async with self._render_gate.hold(STOCK_RENDER_KEY, free=True, stock=True):
                item = await self.songs.produce_song(brief, prefer="slow")
            job.phase = "done"
            self._render_done.set()
            # render the intro BEFORE the song enters the library: the scheduler
            # shares this event loop and would air the song bare in the gap
            intro = await self._render_intro(item, None)  # stock intros are non-fatal
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True, meta=item.get("meta"))
            self._store_intro(intro, song_id, None)
            self._finish_early_art(song_id, art_task)
            attached = True
            self._last_stock_song_at = self.clock.now()
            log.info("song.produced", extra={
                "title": item.get("title"), "genre": item.get("genre"),
                "duration_s": item.get("duration_s")})
            return True
        finally:
            if art_task is not None and not attached:
                art_task.cancel()

    def stock_capped(self) -> bool:
        """True when the library already holds songs.stock_max_library usable
        (non-retired) songs: stock generation pauses so the expensive song
        model only runs for listener requests (§6.1). 0 disables the cap."""
        cap = self.cfg.songs.stock_max_library
        return cap > 0 and self.store.count_usable_of_type("song") >= cap

    async def _produce_request_song(self, req: dict, job: SongJob | None = None) -> bool:
        """Turn one queued listener request into a song (+ short intro). On a
        hard failure, record the attempt (3 max -> failed) and re-raise so the
        loop backs off; a failing intro never blocks the request (4.5/4.6)."""
        log.info("request.producing", extra={"request_id": req["id"]})
        self.store.mark_request_producing(req["id"])
        self.request_stage[req["id"]] = "waiting"  # for its turn at the brief LLM
        paid = bool(req.get("priority"))
        art_task = None
        attached = False
        brief_s = 0.0
        # a benchmark row (docs/systems/queue-bench.md): same claim, same turn
        # at the brief LLM, same render slot; then its audio is thrown away
        bench = _bench_opts(req)
        marks: dict[str, Any] = {"claimed": self.clock.wall()}
        try:
            backlog = len(self.store.queued_requests(
                bury_threshold=self.cfg.voting.bury_threshold))
            # Paid requests are never shortened by the backlog rule (§0.5).
            short = backlog >= 3 and not paid
            reused = None
            if bench is not None and bench.get("replay_of") and bench.get("reuse_lyrics", True):
                # a replay renders the words its original was made from: the
                # brief LLM is not asked again (no turn at the gate either)
                reused = self.store.request_song_brief(int(bench["replay_of"]))
            # turns go in queue order (paid first, then oldest), whoever asked first
            turn = (self._brief_gate.hold(SongJob("request", req).queue_key())
                    if reused is None else _no_gate())
            async with turn:
                self.request_stage[req["id"]] = "writing"
                # Prep Time on the board starts here: lyrics/brief writing, not
                # submission (RADIO §11) — queue idle is not prep work. If the
                # words were written ahead of a render slot, it is re-anchored
                # below so the wait for that slot is not counted either.
                self.store.mark_request_writing(req["id"])
                written_at = self.clock.wall()
                parts = from_row(req)
                marks["write_start"] = written_at
                if reused is not None:
                    brief = dict(reused)
                else:
                    brief = await self.songs.brief(self._genre_context(),
                                                   request_text=req["text"],
                                                   short=short, paid=paid,
                                                   parts=parts)
                marks["write_end"] = self.clock.wall()
                # how long the words took: Prep Time counts it, the wait for a
                # render slot below does not (RADIO §11)
                brief_s = max(0.0, self.clock.wall() - written_at)
                if job is not None:  # inside the lock: the next brief sees it
                    job.genre = brief.get("genre")
            brief["request_id"] = req["id"]
            if bench is not None and bench.get("duration_s"):
                brief["max_duration_s"] = float(bench["duration_s"])
            # start the cover now, in parallel with the song render: the brief
            # already has everything FLUX needs (title/artist/genre/lyrics/style)
            if bench is None:  # a benchmark song is never shown: no cover
                art_task = self._start_early_art(
                    brief, f"req{req['id']}", request_text=req["text"], paid=paid,
                    parts=parts)
            # its words are written: in line for a generator slot (RADIO §11)
            self.request_stage[req["id"]] = "waiting"
            if job is not None:
                job.phase = "render"
            async with self._render_gate.hold(
                    job.queue_key() if job is not None else STOCK_RENDER_KEY,
                    free=not paid):
                # requests take the fastest idle generator (RADIO §6.2)
                self.request_stage[req["id"]] = "generating"
                # Prep Time starts now, back-dated by the brief's own duration:
                # the wait for a slot is queue idle, not prep (RADIO §11)
                self.store.mark_request_writing(
                    req["id"], at=self.clock.wall() - brief_s)
                marks["render_start"] = self.clock.wall()
                item = await self.songs.produce_song(brief, prefer="fast")
                marks["render_end"] = self.clock.wall()
            if job is not None:
                job.phase = "done"
                self._render_done.set()
            if bench is not None:
                return self._finish_bench(req, item, marks, reused is not None)
            # the generator is done; what is left (intro, waiting behind the
            # requests ahead of it) must not read "being generated"
            self.request_stage[req["id"]] = "rendered"
            # Everything awaited is done before the song is stored: song, intro
            # and 'ready' land with no await between them, so the scheduler can
            # never air a request song bare, as stock, before its intro exists.
            intro = await self._render_intro(item, req)
            await self._wait_turn(job)
            item_meta = {**dict(item.get("meta") or {}), "request_id": req["id"],
                         "request_text": req["text"], "paid": paid}
            if parts is not None:  # docs/shipped/prompt-upgrade.md Step 3: art reads this
                item_meta["request_parts"] = {"lyrics": parts.lyrics,
                                              "music": parts.music,
                                              "art": parts.art}
            song_id = self.store.add_item(
                type_="song", media_path=item["media_path"], duration_s=item["duration_s"],
                sample_rate=item.get("sample_rate"), channels=item.get("channels"),
                title=item.get("title"), artist=item.get("artist"), genre=item.get("genre"),
                evergreen=True, fresh=True, meta=item_meta)
            intro_id = self._store_intro(intro, song_id, req)
            self._finish_early_art(song_id, art_task)
            attached = True
            self.store.mark_request_ready(req["id"], song_id, intro_id)
            log.info("request.ready", extra={
                "request_id": req["id"], "song_item_id": song_id,
                "title": item.get("title"), "intro_item_id": intro_id})
            return True
        except LLMTruncated as e:
            # the brief model was cut off (overload/limit), not the request's
            # fault: re-queue for free a few times, then count attempts again
            cuts = self._llm_cuts.get(req["id"], 0) + 1
            self._llm_cuts[req["id"]] = cuts
            if cuts <= LLM_FREE_CUTS:
                self.store.requeue_request(req["id"])
                log.warning("request.llm_cut", extra={
                    "request_id": req["id"], "cuts": cuts, "error": err_text(e)})
                raise
            self._fail_request_attempt(req, paid, e)
            raise
        except httpx.TransportError as e:
            # backend down/dropped the connection: not the request's fault
            self.store.requeue_request(req["id"])
            log.warning("request.backend_unavailable", extra={
                "request_id": req["id"], "error": err_text(e)})
            raise
        except Exception as e:
            self._fail_request_attempt(req, paid, e)
            raise
        finally:
            if art_task is not None and not attached:
                art_task.cancel()

    def _finish_bench(self, req: dict, item: dict, marks: dict[str, Any],
                      reused: bool) -> bool:
        """Close a benchmark request: delete the audio it rendered (the file
        never became a library item, so hard rule 5 does not cover it), record
        the timings on the row. No item, no intro, no upload, nothing airs."""
        with contextlib.suppress(OSError):
            Path(str(item["media_path"])).unlink()
        meta = dict(item.get("meta") or {})
        result = {**{k: round(float(v), 3) for k, v in marks.items()},
                  "reused_lyrics": reused, "audio_s": item.get("duration_s"),
                  "backend": meta.get("backend"), "generator": meta.get("generator"),
                  "title": item.get("title"), "genre": item.get("genre")}
        self.store.finish_bench_request(req["id"], result)
        log.info("bench.done", extra={"request_id": req["id"], **{
            k: result[k] for k in ("audio_s", "backend", "generator", "reused_lyrics")}})
        return True

    def _fail_request_attempt(self, req: dict, paid: bool, e: BaseException) -> None:
        max_attempts = self.cfg.payments.max_attempts if paid else 3
        n = self.store.request_failed_attempt(req["id"], max_attempts=max_attempts)
        log.warning("request.failed", extra={
            "request_id": req["id"], "attempt": n, "error": err_text(e)})
        if paid and n >= max_attempts and not req.get("bench"):
            log.warning("payment.refund_due", extra={
                "request_id": req["id"],
                "payment_intent_id": req.get("payment_intent_id")})

    def _named_intro_text(self, title: str, artist: str) -> str:
        variants = self.cfg.talk.intro_variants or ["Here's {title} from {artist}."]
        return variants[self.rng.randint(0, len(variants) - 1)].format(
            title=title, artist=artist or "the house band")

    async def _render_intro(self, item: dict, req: dict | None) -> dict | None:
        """Render a short DJ intro naming the just-created song. Stock intros
        are code-written and recycle (revision 11: no LLM, every airing of the
        song gets its name said). A request intro credits the listener and MUST
        exist so they hear their request was serviced: two LLM tries, then a
        scripted shout-out rendered directly."""
        title = item.get("title") or "this next one"
        artist = item.get("artist") or ""
        if req is None:
            host = self._current_host()
            text = self._named_intro_text(title, artist)
            try:
                r = await self.voice.render({"text": text}, "intro", 6.0,
                                            voice=host.voice if host else None)
            except Exception as e:
                log.warning("intro.failed", extra={"error": err_text(e)})
                return None
            return {"media_path": str(r["path"]), "duration_s": r["duration_s"],
                    "sample_rate": r.get("sample_rate"),
                    "channels": r.get("channels"), "meta": {"text": text}}
        genre = genres.display(item.get("genre") or "")
        context = f'Next song: "{title}" by {artist} ({genre}).\n'
        if req is not None:
            paid = bool(req.get("priority"))
            intro_parts = from_row(req)
            intro_text = intro_parts.song_text() if intro_parts else req["text"]
            context += f"Listener request: {json.dumps(intro_text)}\n"
            if paid:
                context += ("This song was made for a PAID priority request. OPEN "
                            "with an excited shout-out that it's a priority request "
                            "(e.g. 'Priority request line! You paid, we played!'), "
                            "then name the song. Never mention the amount or any "
                            "name from the request text. ")
            else:
                context += ("This song was made for a listener's request. OPEN with an "
                            "excited shout-out that it's a listener request (e.g. "
                            "'Request line! You asked, we made it!'), then name the "
                            "song. ")
        context += "Do not mention the clock time."
        for _ in range(2 if req is not None else 1):
            try:
                host = self._current_host()
                return await self.voice.produce_item(
                    "intro", 10.0, context=context,
                    voice=host.voice if host else None,
                    host=host.name if host else "Liam")
            except Exception as e:
                log.warning("intro.failed", extra={
                    "error": err_text(e),
                    "request_id": req["id"] if req is not None else None})
        if req is None:
            return None
        if req.get("priority"):
            text = f"Priority request line! You paid, we played! Here's {title}"
        else:
            text = f"Request line! You asked, we made it! Here's {title}"
        text += f", by {artist}!" if artist else "!"
        try:
            r = await self.voice.render({"text": text}, "intro", 6.0)
        except Exception as e:
            log.warning("intro.fallback_failed", extra={
                "error": err_text(e), "request_id": req["id"]})
            return None
        log.info("intro.fallback", extra={"request_id": req["id"]})
        return {"type": "intro", "media_path": str(r["path"]),
                "duration_s": r["duration_s"], "sample_rate": r["sample_rate"],
                "channels": r["channels"], "role": "intro", "meta": {"text": text}}

    def _store_intro(self, intro: dict | None, song_id: int,
                     req: dict | None) -> int | None:
        """Store a rendered intro against its song. Synchronous on purpose."""
        if intro is None:
            return None
        meta: dict = {"song_item_id": song_id,
                      "text": (intro.get("meta") or {}).get("text")}
        if req is not None:
            meta["request_id"] = req["id"]
        return self.store.add_item(
            type_="intro", media_path=str(intro["media_path"]),
            duration_s=intro["duration_s"], sample_rate=intro.get("sample_rate"),
            channels=intro.get("channels"), role="intro", evergreen=req is None,
            fresh=True, meta=meta)

    def _recent_genres(self) -> list[str]:
        """Style families of the last N songs in COMMITTED (aired) order, padded
        with the most recently created songs when the program is short. The
        scheduler's §5.2 rule is about what listeners just heard, not what the
        producer just generated."""
        n = self.cfg.playout.genre_no_repeat
        heard = [g for _, _, g in self._recent_committed_songs(n) if g]
        created = [it["genre"] for it in self.store.list_items("song")
                   if it.get("genre")]
        out: list[str] = []
        for g in created[-n:] + heard:
            if g not in out:
                out.append(g)
        return out[-n:]
