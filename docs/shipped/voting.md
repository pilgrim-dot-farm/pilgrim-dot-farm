# Voting — turkey song ratings and request-queue votes

> *Formerly `VOTING.md`.*

**Status: shipped** (Steps 1–7; Step 8 ships switched off). Amended by
docs/shipped/ratings.md (2026-11-06): programme rows carry the aggregate and
the community average is drawn as pink turkeys under the listener's cyan vote.

Two listener controls with no accounts: RATE THIS SONG (five turkeys, open
only while that song is audible) and ↑ score ↓ on each free request on the
NOW QUEUED board, which orders the free line. Normative behaviour: RADIO §5.2,
§6.1, §9.1, §9.2, §11, §12, §14, §15.

Code: `pilgrim/voting.py`, the vote and rating tables and queries in
`pilgrim/store.py`, `pilgrim/web/rating.js`, `votes.js`, `voter.js`,
`requests.js`, `voting:` and `ratings:` in `pilgrim/config.yaml`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/plans/voting.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.2 Decisions (defaults chosen; the operator may override, §9)

| # | Decision | Why |
|---|----------|-----|
| V1 | **Voter identity** = `voter_id`, a 32-hex random string the page generates once and keeps in `localStorage` (`pd_voter`); sent with every vote. Server stores it with the canonical IP | No accounts; the IP alone would merge a household/NAT; the id alone is trivially reset. Together with V3 this is "basic abuse prevention", not security |
| V2 | **Idempotent upserts.** Request vote: `UNIQUE(request_id, voter_id)`, value ∈ {−1, 0, +1} (0 = retract). Rating: `UNIQUE(epoch, seq, voter_id)` (one per **airing**), stars 1–5, a re-vote while still open replaces it | Change/retract supported; a retry is harmless |
| V3 | **Abuse limits:** (a) the existing per-IP 10-minute limiter, `voting.per_client_per_10min` (60); (b) at most `voting.max_voters_per_ip` (3) distinct `voter_id`s per IP per target; beyond that, 429 | Clearing `localStorage` in a loop buys at most 3 votes per target per address |
| V4 | **Request voting window** = status `queued`, `producing`, `ready`; closes at `aired` (or any terminal status). Votes on `producing`/`ready` rows are display-only (the row's place is fixed, V6) | Paste: "close voting when the request begins airing; never retrospective" |
| V5 | **Paid rows are not votable** (no control shown, server 409 `not_votable`) | They are FIFO by payment time, not in the contest (paste: "preserve paid priority") |
| V6 | **Ordering.** Paid first (unchanged). Then rows already in flight (`producing`/`ready`) by the time they were **claimed**. Then free `queued` rows by **score DESC, created_at ASC**. Ties = today's FIFO, so with no votes nothing changes | Votes pick what is *written next*; a job that started is never reordered; air order of ready requests = claim order (`_wait_turn` and the splice agree) |
| V7 | **Bury:** a free `queued` row with score ≤ `voting.bury_threshold` (**−3**) is skipped by the producer while buried; it stays on the board ("buried") and revives when upvoted. Never auto-evicted (open question Q2) | The point of the feature: garbage is not generated. Reversible; operator-tunable; `null` disables |
| V8 | **Rating window** = the song on air now, by `(epoch, seq)`, with `ratings.grace_s` (**5 s**) after it ends for a click in flight. Not a song, or any other seq → 409 `rating_closed` | Server-authoritative; the client only hides the control. Previously aired songs can't be rated |
| V9 | **Aggregates** are computed from the vote tables (SUM/COUNT/AVG), never cached on `requests`/`items` | Always accurate; retractions and changes need no bookkeeping |
| V10 | **Shown aggregates:** request rows show `score`; the state carries the on-air song's lifetime `{count, avg}` (all airings). The page shows `avg · n` once `count > 0` | Per-airing uniqueness, lifetime display |
| V11 | **Own votes** are remembered client-side (`localStorage`) and confirmed by each POST response; shared GETs never carry per-voter data | Keeps `/api/requests` and `/api/station/state` cacheable at the edge |
| V12 | **Ratings do not change playout in this plan.** An optional, config-gated Step 8 lets the rating weight the recycled-song draw; it ships **off** | Agency first, policy later, by the operator |
| V13 | `voting.enabled` / `ratings.enabled` default **true**; `false` = routes 404, UI hidden | Rollback switch |

### 0.3 Rules

1. Hard rules unchanged: nothing here imports payments; playout never
   waits on anything; the browser is never trusted about status or window
   (server decides open/closed); LLM untouched.
2. `scheduler.py` / `selector.py` / `producer.py` read votes only through
   `Store` queries (pure of time/random). Step 8 is the only playout touch
   and is off by default.
3. Listener text and anything from the vote tables is rendered with
   `textContent`. Request ids are plain integers (they are not secrets; a
   vote needs an open window, not knowledge of the id).
4. Every GET keeps its cache header and stays voter-independent (V11).
5. No new dependency. Schema changes are additive and documented in
   RADIO §14 in the same commit (AGENTS rule 8).

## 8. Optional — ratings weight the recycled-song draw (off by default; RADIO §5.2)

Only after Steps 1–7 ship and only as its own commit. In
`Scheduler._song_weight`, when `cfg.ratings.rotation_weight` is true and
`store.song_rating(item_id).count >= rotation_min_votes`, multiply the
weight by `clamp(avg / 3.0, 0.25, 1.5)` (a 3-turkey song is neutral, a
1-turkey song keeps a quarter of its pull, a 5-turkey song gets half again).
It reads the store only (pure of time/random), never breaks spacing (it
orders songs that already pass it) and never drops a song below the
starvation guard. Sim assertion: with ratings seeded, every §15 invariant
still holds and a 5-turkey song airs more often than a 1-turkey one of the
same age. `config.yaml` ships `rotation_weight: false`; flipping it is the
operator's call (§9 Q3).

## Rollback

- Config: `voting.enabled: false` and/or `ratings.enabled: false` hide the
  UI and 404 the routes with no code change; `bury_threshold: null` keeps
  votes but stops burying.
- Code: `git revert` per step. The schema changes are additive
  (`request_votes`, `song_ratings`, `requests.claimed_at`); old code ignores
  them, nothing is dropped, `station.db` is never deleted or rewritten
  (hard rule 5). A reverted queue order falls back to pure FIFO; nothing in
  flight is affected.
- Edge: the nginx locations are harmless once the origin 404s the routes;
  revert the templates in the same revert and `make edge-up`.

## 9. Open questions for the operator (defaults in force until answered)

1. **Identity strength.** V1/V3 stops casual abuse only. If vote brigading
   appears, the next step is a signed cookie minted by the origin (no
   accounts, still no PII) — not in this plan.
2. **Buried requests** stay on the board and keep a slot in the free line
   (`free_queue_max`). Should a request buried for more than N hours be
   `evicted` (reason `voted down`)? Default: no automatic eviction.
3. **Ratings → rotation** (Step 8) ships off. Turn it on after a week of
   ratings, or leave ratings as display only?
4. **Paid rows**: not votable (V5). Flip to "votable, display-only" if the
   community should at least be able to react to a paid prompt.
5. **Grace** of 5 s after a song (V8): too generous, or fine?
6. **Retention**: `request_votes`/`song_ratings` store the canonical IP like
   `visitor_ips` does; no pruning policy exists for either (RADIO §14).
