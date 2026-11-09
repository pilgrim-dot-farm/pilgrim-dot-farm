# Architecture — Pilgrim Dot Farm Radio

One page that shows how the station fits together and where each part is
documented. `RADIO.md` remains the normative spec; this file is the map, not a
replacement. Section references are to `RADIO.md` unless stated otherwise.

## 1. What the station is

A fictional small-town radio station that makes its own programming. MiniMax
Music 3 renders songs, LLMs write news, commercials, DJ copy and song briefs,
Kokoro speaks it, and MOSS-SoundEffect adds sound effects. Everything is
rendered, QC'd and normalized **before** it can air, so the browser only ever
plays finished files and a listener never waits on a model.

The central design constraint: **playout never waits on production**
(`AGENTS.md` rule 1, §0). Production is a background, demand-driven refill of
inventory; playout is a committed window of already-rendered items.

## 2. Topology

```
listener ── Cloudflare ── ALB ──┬── EC2 edge nginx :8080   (stateless)
                                │        │ tailscale serve (https)
                                │        ▼
                                │   M5 gateway nginx :8090 ── station :5080 (public)
                                │
                                └── ECS Fargate web tier     (stateless)
                                         │ Postgres + Valkey in the VPC
                                         │ LiteLLM via the bastion relay :4000
                                         │
M5 Mac Studio (origin, stateful) ────────┘
  station :5080 public / :5081 admin (DJ console)
  station.db or RDS Postgres, pilgrim/library/, the producers,
  song generation (mlx-serve), FLUX cover art, S3 uploads
```

| Layer | Where | State | Responsibility |
|-------|-------|-------|----------------|
| **Origin** | M5 Mac Studio, launchd (`pilgrim/deploy/mac/origin.sh`) | All of it: media files, DB, `.env` | Playout, production, the authoritative API, media publishing |
| **Gateway** | M5 nginx `127.0.0.1:8090` | none | Terminates the tailnet hop in front of the origin |
| **Edge** | EC2 `pf`, nginx `:8080` | none | Public entry behind Cloudflare → ALB; proxies to the origin over the tailnet (`docs/shipped/split.md`) |
| **Web tier** | Fargate service `pilgrimdotfarm/front-end` | Postgres + Valkey only | Serves the site, programme and heartbeat routes without waking the origin; owns the Sheep Pen chat (`docs/plans/scalability.md`) |
| **Bastion relay** | `pf` `:4000` → `tailscale serve` → LiteLLM | none | Lets the web tier reach LiteLLM (request moderation, paid summaries) |

`RADIO.md` §3 has the program model (a sequence with seq numbers on the
origin's clock), the idle/wake rules (`docs/plans/idle.md`) and the epoch /
revision identity that the DJ console and clients share.

## 3. Components

| Component | File | What it does | Doc |
|-----------|------|--------------|-----|
| Scheduler | `pilgrim/scheduler.py` | Keeps the committed window full on the origin clock; freezes/thaws; splices requests and operator actions | §5, `plans/idle.md` |
| Selector | `pilgrim/selector.py` | Pure function `(state, rng) → next item`; spacing, gravity, freshness, recycling | §5 |
| Producer | `pilgrim/producer.py` | Demand-driven refill of inventory targets; host workers; request queue; song concurrency | §6, `shipped/song-concurrency.md` |
| Pipelines | `pilgrim/pipelines/` | Songs, voice, news, SFX, cover art | §6 |
| Audio | `pilgrim/audio/` | QC, silence trim, two-pass `loudnorm`, FLAC/MP3 encode | §8 |
| Store | `pilgrim/store.py`, `pilgrim/db.py` | One engine-neutral schema on SQLite (tests/sim/dev) and Postgres (prod) | §14 |
| Hot state | `pilgrim/hotstate.py` | Publishes programme/state/health/stages and `player:*`/`wake` keys to Valkey for the web tier | §14 "Hot state" |
| Media publisher | `pilgrim/media_publish.py` | Uploads finished media to the public S3 bucket behind CloudFront | §8.4 |
| Server | `pilgrim/server.py` | Public + admin HTTP listeners; all routes in §11 | §11 |
| Web tier | `pilgrim/web_app.py` | Fargate app: serves site and programme from Valkey, writes heartbeats, hosts the Sheep Pen | §11 "Web tier" |
| Client | `pilgrim/web/` | Web Audio playback (`decodeAudioData` + `start(when)`), one `#sink`, request line, ratings, votes, chat | §9 |
| DJ console | `pilgrim/tui/` | Textual operator console over the admin listener | `systems/console.md` |
| Payments | `pilgrim/payments.py` | Stripe Checkout Sessions + signature-verified webhooks; never imported by playout | `shipped/payments.md` |
| Sheep Pen | `pilgrim/chat/`, `pilgrim/chat_worker.py` | Moderated chat on the web tier; Valkey hot, Postgres audit | `plans/sheep-pen-brief.md` |
| Deploy | `pilgrim/deploy/` | Render + install origin, gateway, edge, container, bastion | `pilgrim/deploy/README.md`, `RUNBOOK.md` |

## 4. Content and production classes

Every item is **evergreen** (songs, commercials, liners, IDs, promos, named
song intros — produced ahead, recyclable), **expiring** (news, clock reads,
day-level promos — refreshed, never recycled) or **contextual** (DJ talk-ups,
field reports — made just in time, never recycled). News and contextual items
are never recycled; that is enforced in code, not prompts (`AGENTS.md` rule
10). `RADIO.md` §4 lists every segment type, including SFX as sidecar overlays
that never air alone and never ride on news (`docs/systems/sfx.md`).

## 5. Data flow

```
config.yaml ─▶ producer ─▶ brief/lyrics (LLM) ─▶ MiniMax (song) / Kokoro (voice)
                         ─▶ QC ─▶ normalize ─▶ library/<item>.mp3
                         ─▶ store: items (+ metadata, art, expiry)
                         ─▶ media publisher ─▶ S3 ─▶ CloudFront
                                                    │
scheduler ─▶ selector ─▶ committed program rows ─────┤ GET /api/station/program
                                                    ▼
browser ─▶ fetch(url) ─▶ decodeAudioData ─▶ start(when) ─▶ heartbeat {seq, position}
                                                    │
                                       airplay ledger + listener count
```

Two rules shape the client: all audio goes through the Web Audio graph with one
persistent `#sink` (`AGENTS.md` rule 9), and every media fetch has a stall
timeout with an origin fallback, because a hung CloudFront connection is dead
air (`AGENTS.md` §10).

## 6. Storage in one paragraph

One schema, two engines. `pilgrim/store.py` writes every statement once;
`pilgrim/db.py` translates for Postgres when `DATABASE_URL` is set. SQLite
(`station.db`) is for tests, simulation and the single-host dev run; Postgres
(RDS) is production, written by the Mac origin and read/written by the web
tier. Valkey (ElastiCache) holds only hot, disposable documents — the
programme, station state, health, request stages, player heartbeats, the wake
flag, and the Sheep Pen's streams, counters and tombstones. `RADIO.md` §14 is
the table-by-table reference, including the Sheep Pen key layout and the
cluster-slot rules.

## 7. Invariants that shape every design

| Invariant | Where | Consequence |
|-----------|-------|-------------|
| Playout never waits on production | `AGENTS.md` 1 | Only rendered, QC'd items enter the committed window; missing contextual items are replaced by a liner |
| Tests never call real backends | `AGENTS.md` 2 | `pilgrim/tests/fakes/` mirrors every backend, with failure injection |
| No ambient time/randomness in scheduler/selector/producer | `AGENTS.md` 3 | Injected `Clock` and RNG make the 24-hour simulation possible |
| Config is the only source of hosts, ports, models, weights | `AGENTS.md` 4 | `pilgrim/config.yaml` + overlay for containers |
| News and contextual items are never recycled | `AGENTS.md` 10 | Enforced in code |
| Web Audio only, one sink | `AGENTS.md` 9 | No `<audio>` chains; no per-item elements |
| Payments never touch playout; the browser is never trusted about money | `AGENTS.md` 11 | Webhook or server-side session retrieve marks paid; comps need the operator token |
| Idle monitoring must not wake the station | `AGENTS.md` §10 | `/api/health` and `/api/station/state` are safe probes; `/api/station/program` is a wake signal |

## 8. Environments and checks

| Environment | How | State |
|-------------|-----|-------|
| Unit tests | `make test` | SQLite in a temp dir, fakes only |
| Simulation | `make sim` | 24 simulated hours, every §15 assertion |
| Postgres parity | `make pg-test` | Throwaway Postgres container, same suite |
| Client e2e | `make e2e` (local), `make e2e-split` (through the edge stack) | Playwright, fakes, short items |
| Smoke | `make seed`, `make smoke`, `make pay-smoke`, `make media-smoke` | Real backends, explicitly requested only |

## 9. Glossary

- **Item** — one rendered piece of audio in the library (`items` table), with
  type, metadata, duration, expiry and S3 keys.
- **Program / committed window** — the future sequence of items the scheduler
  has committed, identified by `seq`; never reused.
- **Epoch / revision** — process identity and programme identity. A stale
  client cursor gets `reset: true`.
- **Committed lookahead** — how far ahead the scheduler keeps the window
  rendered (`committed_lookahead_s`).
- **Splice** — placing a ready request or operator action into the next legal
  slot without waiting on production.
- **Hot state** — the Valkey documents the web tier serves instead of calling
  the origin.
- **Frozen / thawed** — the idle policy: the playhead stops, the window stays
  playable, and PLAY or a heartbeat resumes it.
- **Prep time** — from the start of lyrics/brief writing to the earlier of
  airing and the scheduled `due_at`; never queue idle.

## 10. Where to read next

- Normative behaviour, API, config, storage, tests: `RADIO.md`.
- Build rules and commands: `AGENTS.md`.
- Backend reality (shapes, limits, failure modes): [backends.md](backends.md).
- Model roles: [models.md](models.md).
- Active work: `docs/plans/`; completed plans: `docs/shipped/`; subsystem
  references: `docs/systems/`; ideas: `docs/backlog/`.
- Deploy operations: `pilgrim/deploy/README.md` and `pilgrim/deploy/RUNBOOK.md`.
- Station voice and canon: `pilgrim/bible/`; prompts: `pilgrim/prompts/`.
