# Ratings fix — community average as pink turkeys under your cyan vote

> *Formerly `FIX_RATINGS.md`.*

**Status: shipped** (Steps 1–5, 2026-11-06). Amends docs/shipped/voting.md.

Fixed the web tier's rating window on the grace path, put each song's
lifetime aggregate on its programme row, and drew the community average as
fractional pink turkeys under the listener's own cyan vote. Normative
behaviour: RADIO §9.1, §9.2, §11, §14. Code: `pilgrim/voting.py`,
`pilgrim/web_app.py`, `pilgrim/hotstate.py`, `pilgrim/web/rating.js`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/plans/ratings.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.1 What was found (investigation of 2026-11-06)

- **Ratings are retained.** Production Postgres holds 131 `song_ratings`
  rows: 99 migrated from the old `station.db` (per-day counts match exactly)
  and 32 cast on 2026-11-06 through the Fargate web tier. The lifetime
  aggregate (`Store.song_rating`) is keyed by `item_id` and is correct.
- **The UI forgets on purpose.** docs/shipped/voting.md V2/V8 make a rating belong to one
  *airing* `(epoch, seq)`. `rating.js` remembers a rating under
  `pd_rating:<epoch>:<seq>`, so the next airing of the same song shows empty
  turkeys. The only trace of earlier ratings is the dim `avg · n` text, and
  it is shown only while the server says the window is open and is blanked
  the moment the window closes. Of 130 rated songs only one was ever rated
  on a second airing, so the operator has almost never seen an aggregate
  carry over. This is what reads as "not retaining".
- **Bug A (confirmed).** `pilgrim/web_app.py` `CachedScheduler` has no
  `_cum` and no `position()`, but `voting.rating_window` reads both in its
  grace branch (the on-air row has moved past the song). On the web tier a
  rating that lands after the published snapshot has moved on raises
  `AttributeError`, FastAPI answers 500, the client shows "TRY AGAIN". The
  browser plays 1–3 s behind the server (it joins after fetch + decode), so
  clicks in the last seconds of a song hit this. No test covers it.
- **Bug B (by reading).** `rating.js` `poll()` treats one state response
  whose `current_seq` differs from the audible song, or whose `rating.open`
  is false, as "the song is over" and disables the row until the next
  `pd:nowplaying`. The published snapshot is up to one publish tick old plus
  one second of `max-age=1` cache, so a client slightly ahead of the server
  loses the control for the whole song right at song start.

### 0.2 Decisions in force (operator request of 2026-11-06 + defaults)

| # | Decision | Why |
|---|----------|-----|
| D1 | **Always show the aggregate.** Every song item in `GET /api/station/program` carries `rating_count` (int) and `rating_avg` (float or null). The client paints `avg · n` the instant the song becomes audible, keeps it while the song is audible even after the server's window closes, and refreshes it from the 5 s state poll while open. With zero ratings it shows `NO RATINGS YET` | The aggregate must not depend on a poll racing the playhead |
| D2 | **Pink = the community average, always; cyan = your vote, on top.** The turkeys are filled in magenta ("pink") to the community average with **fractional fill**: turkey *i* (1-based) shows the fraction `clamp(avg - (i-1), 0, 1)` of its glyph, clipped left to right (3.4 = three full pink turkeys and 40 % of the fourth). Nothing is pink when `count` is 0. The pink is never hidden by the listener's own input. Once this browser rates the airing (or while hovering/focusing a turkey), its own level is drawn in cyan as whole turkeys **over** the pink; pink stays visible wherever the cyan does not reach. The exact `avg · n` text sits beside the turkeys. Cyan is restored per airing from `pd_rating:<epoch>:<seq>` as today | "Eyes on the community outlook"; "when the user votes, their cyan overshadows the pink" (operator, 2026-11-06). Voter-independent data, so no new API: `/api/station/state` stays a shared `max-age=1` cache (RADIO §11) |
| D3 | **The per-airing rule stays.** `UNIQUE(epoch, seq, voter_id)`, one rating per airing, window = the on-air song + `ratings.grace_s` (docs/shipped/voting.md V2, V8). No schema change | Already shipped and correct |
| D4 | **Bug A fix**: `CachedScheduler` gets `_cum` and `position()` with the same meaning as `Scheduler`'s | Make `rating_window` correct over the snapshot instead of special-casing it |
| D5 | **Bug B fix**: the poll closes the row only when the server has moved **past** the song (`current_seq > seq`), the epoch changed, or ratings are off. A snapshot **behind** the playhead is ignored. A closed row keeps its paint and aggregate until the next `pd:nowplaying` | A one-second-old snapshot must never kill the control |
| D7 | **No per-song browser memory.** Nothing new goes into localStorage; the only key stays `pd_rating:<epoch>:<seq>` | The community average is the "existing rating"; a rating from an earlier airing is already inside it |
| D6 | No change to `POST /api/station/rating`, `song_ratings`, `/api/station/state` or the admin audit route | Scope |

## 6. Open questions for the operator (defaults in force until answered)

1. **Your own earlier rating.** After rating a song, its next airing shows
   only the community pink (your vote is inside the average), not your
   earlier cyan (one rating per airing, docs/shipped/voting.md V2). Showing *your* past
   rating across airings or devices would need a per-voter route and is not
   in this plan. Want it?
2. **Pink glow.** The magenta layer has the same soft drop-shadow glow as the
   cyan one. If the two glows blur together at phone size, drop the glow on
   the magenta layer (one CSS line in §3.3).
3. **Possibly lost ratings on the evening of 2026-11-05.** Between the
   Fargate web tier going live and the Mac's Postgres cutover (~22:14 EST),
   ratings posted through the web tier went to Postgres while the Mac's
   published state still read SQLite, and a `pg-migrate --truncate` would
   have wiped them. Postgres has no rows in that window; whether none were
   cast or they were truncated cannot be told from the data. Did you rate
   anything that evening before the 22:49 EST restart?
4. **`max_voters_per_ip: 3`.** Three distinct browsers behind one home IP
   can rate one airing; a fourth gets 429 "TRY AGAIN". A private window
   generates a new voter id every session. Raise it?
