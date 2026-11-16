# Fix radio — request priority without starving the song rotation

> *Formerly `FIX_RADIO.md`.*

**Status: shipped** (2026-10-31, operator chose Option A).

`playout.request_hold_max_s` defaults to `0`: a ready request splices into
the next song slot and stock songs are never held back for a pending request
(RADIO §5.2). Code: `pilgrim/scheduler.py`, `pilgrim/config.yaml` (`playout`).

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/fix-radio.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

## 0. Evidence (2026-10-31)

- 18:20:52 request 483 ("A squirrel took my keys") approved → `request.producing`.
- 18:26:16 → 18:31:22: **5 min 06 s of pure voice** (7 dj_talk, ~14 liners,
  2 news, 2 commercials, zero songs) while the M5 render ran (175.8 s song,
  427 s wall). Request aired 18:31:22.
- Every request today caused the same pattern: 9 song-free gaps with a client
  connected, **~69 min of ~402 min client-connected air (17%)** voice-only.
  Baseline song share with no pending request: **85–100%** (e.g. 01:57–03:42: 97%).
- Real production times observed: 10–26 min (qwen38 brief ~3–170 s + mlx render
  208–427 s + QC/intro). The hold cap (`request_hold_max_s`) is 900 s — the
  design was validated on fakes where production takes seconds.

## 1. Root cause

`Scheduler.commit_lookahead()` (`pilgrim/scheduler.py`): while
`_request_hold()` is true (any request queued/producing/ready-and-uncommitted,
capped per request at `playout.request_hold_max_s` = 900 s),

1. the fill target drops from `committed_lookahead_s` (300 s) to
   `filler_horizon_s` (60 s), and
2. **stock songs are blocked** (`st.available["song"] = False`), with a
   song fallback only when coverage < 60 s (dead air).

That is the voice diet. The hold is **redundant**: `place_request_next()`
splices a ready request at the *first future item* (right after what is
playing), inserts the liner/callout bridge the no-song-after-song and
words-between-songs rules require, renumbers future seqs with fresh high-water
seqs, and bumps the programme revision; revision-aware clients cancel obsolete
future sources and refetch the snapshot. A committed stock song ahead of the
splice point does **not** delay the request — it is pushed back by the
renumber. The hold's only effect is what the stock songs would have filled.

## 2. Target behavior

| When | Committed window | Request priority |
|------|------------------|------------------|
| Request queued / producing | Fills normally to `committed_lookahead_s` **including stock songs** (normal rotation, spacing, weighting — unchanged) | — |
| Request ready (song + intro rendered) | Splice at next song slot, **ahead of committed stock songs**, intro glued in front, liner bridge where required, revision bump | Unchanged — same as today |
| Request stuck (backend down) | Fills normally; nothing to release | The 900 s cap and its release path disappear with the hold |

Listener-facing delta: while a request renders, the station sounds like the
station (music rotation) instead of a voice diet. The request still cuts in at
the next boundary the moment it is rendered. Nothing else changes.

## 4. Known deltas / risks

- **Client cutovers become more frequent.** Splices now routinely cut in front
  of committed stock songs the client already fetched. Same mechanism as today
  (revision bump → cancel future sources → refetch snapshot, docs/systems/console.md §5/§9.2,
  verified in the 2026-10-28 work); no API change.
- **Pushed-back stock carries a stale `commit_song_air`** until step 9a lands:
  bounded skew of a few minutes (a splice inserts ~3–4 min of audio).
  Acceptable; step 9a removes it.
- **FIFO inversion window** (step 9b): theoretical today, stays theoretical
  unless the guard lands — production is strictly sequential, so two requests
  are never ready-uncommitted simultaneously in normal operation.
- **Out of scope** (from the incident review, tracked separately): identical-
  text request dedupe (482/483 "A squirrel took my keys"), the 00:49:05 server
  hard death (no traceback, no OOM in journal), and qwen38 empty-content retry
  tuning (170 s brief, 3× empty moderation calls that rejected a harmless request).
