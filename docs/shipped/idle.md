# Idle — the farm sleeps when nobody is listening

> *Formerly `IDLE.md`.*

**Status: shipped** (Steps 1–5).

With no player heartbeat for `idle.after_s`, playout freezes with a full
committed window already rendered and production pauses (listener requests
excepted). The first `GET /api/station/program` or heartbeat wakes it: the
frozen window resumes, stale time-bound items are dropped, producers pick up
again. On the web tier the wake is asynchronous (docs/pending/scalability.md
Step 4). Normative behaviour: RADIO §3, §5.3, §6.1, §11, §12, §14; AGENTS §10
(a `/api/station/program` probe wakes the station).

Code: `idle:` in `pilgrim/config.yaml`, `Scheduler.freeze`/thaw in
`pilgrim/scheduler.py`, the producer pause in `pilgrim/producer.py`, the
policy in `pilgrim/server.py`, the wake flag in `pilgrim/hotstate.py`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/plans/idle.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.2 Decisions (defaults chosen; the operator may override)

| # | Decision | Why |
|---|----------|-----|
| D1 | Idle = `active_listeners() == 0` for `idle.after_s` (default **120 s**) | A reload or a short network blip must not freeze the station; 120 s is eight missed heartbeats past the 15 s timeout |
| D2 | Wake = `GET /api/station/program` or `POST /api/station/heartbeat` (and any `/api/admin/dj/*` mutation). **Never** `/api/health`, `/api/station/state`, `/api/requests`, `/api/visitors`, media | Those are polled by idle pages, bots and smoke checks |
| D3 | While idle the **playhead freezes** and the committed window stays as it is (≤ 300 s, already rendered: "a song queued up") | Instant resume; nothing to render on wake |
| D4 | On wake, future rows whose `expires_at` has passed for their new slot, and every future `time_check`, are **dropped**; a song-after-song gap that this opens is bridged with `_pick_callout_fallback()`; the revision bumps | No stale news or wrong clock read; songs/liners/commercials are evergreen and keep |
| D5 | Dropped one-shots (news, DJ talk, promos, time checks) are **not** returned to the pool | Same as cancelled cutovers, RADIO §5.5 |
| D6 | Producers pause in idle: voice refill, news, SFX pool, **stock** songs. **Listener requests (free and paid) still produce** in idle | A requester is waiting on it; a paid one has paid. Stock/talk only serves airtime nobody hears |
| D7 | S3 publisher, art worker, phrase manager keep running | Self-limiting; art backfill in quiet hours is a feature |
| D8 | Ready requests still **splice** into the frozen window | They take the next slot the moment someone wakes it |
| D9 | `idle.enabled: true` by default; `false` restores today's behaviour exactly | Rollback switch |
| D10 | Idle state is visible: admin `/api/health` `idle: {idle, since, wakes}`, public `/api/station/state` `idle: bool`, log events `station.idle` / `station.wake` | Operator can see it; TUI can show it. `on_air` stays `true` while frozen (coverage > 0), so PLAY is never disabled by idle |

### 0.3 Rules

1. Idle must never produce dead air. A wake returns a playable program in
   the same request; `wake()` is synchronous and only touches already
   rendered inventory (hard rule 1).
2. `/api/health`, `/api/station/state`, `/api/requests`, `/api/visitors`,
   `/api/media/*` never change idle state (D2). Add a test for each.
3. The sim and e2e harnesses never freeze (they call no `freeze()`).
4. No new dependency. No schema change. No HTTP shape removed; only fields
   added (update RADIO §11, §12 in the same commit, AGENTS rule 8).

## Rollback

`idle.enabled: false` in `config.yaml` restores today's behaviour without a
code change. Full rollback is `git revert` of the Step 1–5 commits; there is
no schema change and no persisted idle state.

## Open questions for the operator

1. `after_s` = 120 s. Shorter saves more compute; longer avoids a freeze
   during a listener's page reload. Decide after watching the logs for a day.
2. Should **free** requests also pause in idle (only paid produce)? This plan
   says no (D6): a free requester is on the page waiting for "up next".
3. Should the station also stop the **S3 publisher / art worker** in idle to
   keep the M5 fully quiet? This plan says no (D7).
