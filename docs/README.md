# Documentation — Pilgrim Dot Farm Radio

Three files at the repo root outrank everything here:

- **`RADIO.md`** — the spec and the source of truth: behaviour, the HTTP API
  (§11), configuration (§12), storage (§14), tests (§15).
- **`AGENTS.md`** — the rules for agents working in this repo.
- **`PENDING.md`** — what is planned and not built.

If a doc here disagrees with `RADIO.md` or `AGENTS.md`, they win. Report the
conflict.

## Reading order for a new agent

1. `RADIO.md` §0 (principles), §3 (architecture), §4 (content types), §5.2
   (playout).
2. [architecture.md](architecture.md) — the one-page system map.
3. [backends.md](backends.md) — what the external services actually return.
4. The reference for the area you are touching (`systems/`, `shipped/`).

## The map

| Path | What it covers |
|------|----------------|
| [architecture.md](architecture.md) | Topology, components, data flow, invariants, glossary |
| [backends.md](backends.md) | Probe records for every external backend: shapes, limits, failure modes |
| [models.md](models.md) | Which model does what, and where it runs |
| [instagram.md](instagram.md) | Runbook: posting an aired song to Instagram Reels (`make reel-post`) |
| [systems/model-fleet.md](systems/model-fleet.md) | The model hosts, `fleetctl`, `pg-<service>` tmux sessions, the restart watchdog |
| [systems/queue-bench.md](systems/queue-bench.md) | The live queue benchmark (`make queue-bench`) |
| [systems/console.md](systems/console.md) | The Textual DJ console and its admin routes |
| [systems/sfx.md](systems/sfx.md) | The SFX backend, the two pools, the prompt guide (§11) |
| [systems/search.md](systems/search.md) | Web search through the Brave Search API |
| [systems/aws-access.md](systems/aws-access.md) | Reading or changing the station's AWS account from this checkout |
| [request-analysis-20261003/report.md](request-analysis-20261003/report.md) | Measured listener-request traffic |
| `../pilgrim/deploy/README.md`, `RUNBOOK.md` | Deploy operations |

### Shipped

One short record per finished plan: what shipped, where the code is, and the
decisions that still explain the code. The step-by-step plans are in git; each
record names the commit.

| Record | What shipped |
|--------|--------------|
| [shipped/split.md](shipped/split.md) | Origin, gateway and edge; public and admin listeners |
| [shipped/song-concurrency.md](shipped/song-concurrency.md) | Three songs rendering at once across the music pool |
| [shipped/announcer-overhaul.md](shipped/announcer-overhaul.md) | DJ talk variety, clock accuracy, self-promotion |
| [shipped/fix-radio.md](shipped/fix-radio.md) | Requests splice in; the rotation is never held for one |
| [shipped/priorities.md](shipped/priorities.md) | Fresher songs first; the persisted airplay ledger |
| [shipped/payments.md](shipped/payments.md) | Paid priority requests through Stripe |
| [shipped/prompt-upgrade.md](shipped/prompt-upgrade.md) | The three-box paid request |
| [shipped/album-art.md](shipped/album-art.md) | A cover for every song |
| [shipped/instagram.md](shipped/instagram.md) | Reels from aired songs |
| [shipped/idle.md](shipped/idle.md) | Freeze and pause when nobody is listening |
| [shipped/voting.md](shipped/voting.md), [shipped/ratings.md](shipped/ratings.md) | Turkey ratings and request votes |
| [shipped/ui-refactor.md](shipped/ui-refactor.md) | The app shell and the paid-flow rounds |
| [shipped/sheep-pen.md](shipped/sheep-pen.md) | The listener chat |
| [shipped/monitoring.md](shipped/monitoring.md) | In-process `/metrics` on both tiers |

### Pending

`PENDING.md` is the list. The long documents behind it:

| Document | What it is |
|----------|------------|
| [pending/agent-wiki.md](pending/agent-wiki.md) | Station memory and the public wiki: a full work plan, parked |
| [pending/scalability.md](pending/scalability.md) | The VPC move; Steps 6, 7 and the end of 9 are open |
| [pending/monitoring-infra.md](pending/monitoring-infra.md) | The parked whole-stack monitoring and alerting design |
| [pending/cgm.md](pending/cgm.md), [pending/sfx-bug.md](pending/sfx-bug.md) | SFX: one sound per animal; the 2026-11-10 bug report |
| [pending/fix-prompting.md](pending/fix-prompting.md) | Song prompting and genre variety |

## Where the code lives

| Area | Files | RADIO.md |
|------|-------|----------|
| Playout | `pilgrim/scheduler.py`, `pilgrim/selector.py` | §5 |
| Production | `pilgrim/producer.py`, `pilgrim/pipelines/` | §6 |
| Station bible, running gags | `pilgrim/bible/`, `pilgrim/promotions.py` | §7 |
| Audio QC, normalization, MP3 | `pilgrim/audio/` | §8 |
| Media publishing, cover art | `pilgrim/media_publish.py`, `pilgrim/album_art.py` | §8.4 |
| Client | `pilgrim/web/` | §9 |
| Voices | `voices:` in `pilgrim/config.yaml`, `pilgrim/prompts/` | §10 |
| Server | `pilgrim/server.py` | §11 |
| Web tier | `pilgrim/web_app.py` | §11 "Web tier" |
| Config | `pilgrim/config.yaml`, `pilgrim/config.py` | §12 |
| Storage | `pilgrim/store/`, `pilgrim/db.py`, `pilgrim/hotstate.py` | §14 |
| Paid requests | `pilgrim/payments.py` | §11 |
| Sheep Pen chat | `pilgrim/chat/`, `pilgrim/chat_worker.py` | §11, §14 |
| DJ console | `pilgrim/tui/` | `systems/console.md` |
| Deploy | `pilgrim/deploy/` | — |

## Conventions

- Paths are repo-root-relative (`docs/shipped/idle.md`). Citations look like
  `RADIO §5.2` or `docs/shipped/voting.md V9`.
- A plan is written under `docs/pending/` and listed in `PENDING.md`. When its
  last step lands, the behaviour goes into `RADIO.md`, the plan is cut down to
  a record in `docs/shipped/`, and its line leaves `PENDING.md`.
- `docs/backends.md` stays factual: probe first, then write down what you saw.
- **Prep time** means the span from the start of lyrics or brief writing to
  the earlier of airing and the scheduled `due_at`. Queue time is not part of
  it.

### Legacy names

Code comments and commit messages cite plans by their old ALL-CAPS names
(`OVERHAUL 4.4`, `SPLIT_PLAN §1.6`, `PAYMENT §3.4`). Section numbers refer to
the full plan in git, named at the top of each record.

| Cited as | Now |
|----------|-----|
| `ALBUM_ART` | [shipped/album-art.md](shipped/album-art.md) |
| `OVERHAUL`, `ANNOUNCER_OVERHAUL_PROPOSITION` | [shipped/announcer-overhaul.md](shipped/announcer-overhaul.md) |
| `FIX_RADIO` | [shipped/fix-radio.md](shipped/fix-radio.md) |
| `FIX_RATINGS` | [shipped/ratings.md](shipped/ratings.md) |
| `IDLE` | [shipped/idle.md](shipped/idle.md) |
| `INSTAGRAM` | [shipped/instagram.md](shipped/instagram.md) |
| `MONITORING` | [shipped/monitoring.md](shipped/monitoring.md); before 2026-11-11, [pending/monitoring-infra.md](pending/monitoring-infra.md) |
| `PAYMENT` | [shipped/payments.md](shipped/payments.md) |
| `PRIORITIES` | [shipped/priorities.md](shipped/priorities.md) |
| `PROMPT_UPGRADE` | [shipped/prompt-upgrade.md](shipped/prompt-upgrade.md) |
| `SHEEP_PEN`, `SHEEP_PEN_PLAN` | [shipped/sheep-pen.md](shipped/sheep-pen.md) |
| `SONG_GEN_CONCURRENCY_PLAN` | [shipped/song-concurrency.md](shipped/song-concurrency.md) |
| `SPLIT_PLAN` | [shipped/split.md](shipped/split.md) |
| `UI_REFACTOR`, `UI_REFACTOR_FEEDBACK` | [shipped/ui-refactor.md](shipped/ui-refactor.md) |
| `VOTING` | [shipped/voting.md](shipped/voting.md) |
| `AGENT_WIKI` | [pending/agent-wiki.md](pending/agent-wiki.md) |
| `SCALABILITY` | [pending/scalability.md](pending/scalability.md) |
| `CGM`, `FIX_PROMPTING` | [pending/cgm.md](pending/cgm.md), [pending/fix-prompting.md](pending/fix-prompting.md) |
| `BRAVE` | [systems/search.md](systems/search.md) |
| `SFX`, `SFX_PROMPTING` | [systems/sfx.md](systems/sfx.md) |
| `TUI` | [systems/console.md](systems/console.md) |
| `MODELS` | [models.md](models.md) |
| `AWS_MCP_USAGE` | [systems/aws-access.md](systems/aws-access.md) |
