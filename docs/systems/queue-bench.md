# Queue benchmark — stress the live queue without filling the library

Status: reference, built 2026-11-14. Code: `pilgrim/tools/queue_bench.py`,
`POST|GET|DELETE /api/admin/bench` (RADIO §11), the `bench` branch in
`producer._produce_request_song` (RADIO §6.1), `requests.bench` (RADIO §14).
Companion: `docs/systems/model-fleet.md` §6 — a benchmark is the load most
likely to knock a model over, and the fleet watchdog is what puts it back.

## 1. What it is

A benchmark request is an ordinary `requests` row with `bench=1`. It is
inserted through the admin listener, then the **production** station works it
exactly like a listener's request: same queue order, same turn at the brief
LLM (qwen38), same render slots on the MiniMax pool. When the render returns,
the audio is deleted and the row is closed as `bench_done` with its timings.

| | Listener request | Bench request |
|---|---|---|
| Enters by | public `POST /api/requests` / paid checkout | `POST /api/admin/bench` (loopback, DJ token) |
| Moderation, Stripe, rate limits | yes | no (operator text; a paid one is shaped like a comp) |
| Queue order, brief gate, render slots | yes | **yes, the same ones** |
| Cover art, DJ intro | yes | no |
| Library item, S3 upload, airs | yes | **no — audio deleted** |
| Public board, hit counter, free cap | yes | no |
| Left behind | request row, song, art | one flagged request row (+ votes), until purged |

## 2. What it costs while it runs

- **Real compute.** Every bench song is a real render (AGENTS rule 6,
  operator exception). Full length is the default; `--short` asks for
  `bench.short_duration_s` (30 s).
- **It shares the queue with listeners.** A paid bench request goes ahead of
  a listener's free request; free bench requests take free slots. Run it in a
  quiet window. `purge --apply` cancels whatever is still queued.
- **The web tier must be current.** The Fargate web tier has its own copy of
  `store.py`; deploy an image built after 2026-11-14 first, or bench rows show
  on the public board.

## 3. Running one

```
make queue-bench BENCH_ARGS="run --name fri1 --free 6 --paid 2 --short"
make queue-bench BENCH_ARGS="report --name fri1 --wait"
make queue-bench BENCH_ARGS="purge"            # dry run: counts
make queue-bench BENCH_ARGS="purge --apply"
```

`run` options:

| Flag | Meaning |
|---|---|
| `--name` | run label (letters, digits, `.`, `_`, `-`); the report filters on it |
| `--free N` | N free requests with stock prompts. Request *k* gets *k* up-votes, so vote order is the reverse of arrival order (`--no-votes` turns that off) |
| `--paid M` | M paid requests, added `--paid-after` seconds (default 30) after the free ones |
| `--replay 555,556` | replay those past requests (their text, and the words their song was made from) |
| `--replay-last N` | replay the N newest aired listener requests |
| `--rewrite-lyrics` | replays ask qwen38 for new words instead of reusing the old ones |
| `--short` | 30 s renders instead of full length |

Replays answer "how do we avoid writing lyrics again": a replay renders the
stored brief of the original song (`items.meta_json.brief`) and takes no turn
at the brief LLM. Use them to load the generator pool alone; use `--free` /
`--paid` (or `--rewrite-lyrics`) to load qwen38 too.

## 4. What the report checks

`report` reads the rows back and prints PASS/FAIL for the queue rules, then
timings.

| Check | Rule (RADIO §6.1) | How it is measured |
|---|---|---|
| `votes_order_free_queue` | Votes order the free requests still waiting | When a free row was claimed, no other free row waiting at that moment had a higher score |
| `paid_before_waiting_free` | A paid request goes ahead of every free request not yet being worked on | No free row is claimed after a paid row arrived and before that paid row |
| `briefs_at_once` | Briefs written at once ≤ `models.max_concurrency[briefs model]` | Peak overlap of `write_start..write_end` |
| `renders_at_once` | Renders at once ≤ healthy generator slots | Peak overlap of `render_start..render_end` |
| `free_renders_at_once` | Free renders at once ≤ `songs.free_request_slots` | Same, free rows only |

Limits come from the running station (`GET /api/admin/bench` → `limits`).
`render_slots` is the pool's capacity *now*: if a generator went down
mid-run the limit shown is the lower one, so a `renders_at_once` FAIL right
after an outage needs a look at the timestamps before it is believed.

Timings: `write` (brief LLM), `wait_for_slot` (words written → render
start), `render`, `request_to_done`, plus audio seconds, songs/hour and which
generator made each song. Only listener and bench requests in flight together
are covered; stock songs rendering at the same time take slots too and are
not in the report (pause them by leaving the library at
`songs.stock_max_library`, which it normally is).

## 5. Where the trash is, and how it goes away

- **Database:** rows with `bench=1` in `requests`, and their
  `request_votes` (voter ids `bench-<run>-…`). `purge --apply` deletes both.
  A row in `producing` is kept until it finishes — purge again afterwards.
- **Library, S3, CloudFront:** nothing. The rendered file is deleted before
  any item is stored.
- **Metrics:** bench work shows in `pilgrim_request_stage`,
  `pilgrim_song_render_seconds` and the render-rate gauges, as intended — that
  is how the run is watched in Grafana.
- **Logs:** `bench.added`, `bench.done`, and the usual `request.producing` /
  `song.produced` lines with the bench `request_id`.

## 6. Limits of the method

- Moderation, the copied-lyrics screen, Stripe and the public rate limits are
  not exercised: bench rows start at `queued`.
- Cover art (FLUX) and the DJ intro (Kokoro) are skipped, so those backends
  see no extra load.
- The checks compare claim order with the *final* vote score. Bench votes are
  cast at insert time and do not change, so this is exact for bench rows; a
  listener voting on nothing changes that (bench rows are not on the board).
