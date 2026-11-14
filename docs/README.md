# Documentation — Pilgrim Dot Farm Radio

This directory is the documentation set for the station. Two files at the repo
root outrank everything here:

- **`RADIO.md`** — the normative spec. It is the source of truth for
  behaviour, the HTTP API (§11), the config schema (§12), storage (§14) and
  the tests (§15).
- **`AGENTS.md`** — the rules for agents working in this repo: hard rules,
  build order, commands, reporting.

If a doc here disagrees with `RADIO.md` or `AGENTS.md`, they win. Report the
conflict instead of silently following the doc.

Start with [architecture.md](architecture.md) for the one-page system map, then
[docs/backends.md](backends.md) for what the external services actually return.

## Conventions

- Paths are **repo-root-relative** (`docs/plans/idle.md`), the same convention
  `RADIO.md` and `AGENTS.md` use. Section citations look like
  `docs/plans/voting.md V9` or `RADIO.md §5.2`.
- Every doc opens with a status note: **active plan**, **shipped**,
  **reference** or **backlog**.
- Shipped plans are kept as history and rationale. They are not rewritten when
  the code drifts; a short status note at the top records what actually
  shipped.
- When the last step of a plan lands, the plan moves from `docs/plans/` to
  `docs/shipped/` and gains a status line. Content stays; only the location and
  the status change.
- `pilgrim/deploy/`, `pilgrim/bible/` and `pilgrim/prompts/` keep their own
  READMEs and prompt files next to the code they describe.

### Legacy names

Code comments, commit messages and `RADIO.md` still cite some docs by their
old ALL-CAPS filename (for example `SPLIT_PLAN §1.6`, `PRIORITIES §4`, `TUI §5`,
`OVERHAUL 4.4`). Each moved doc records its former name under its title; the
full mapping is:

| Legacy name | Now |
|-------------|-----|
| `ALBUM_ART.md` | [shipped/album-art.md](shipped/album-art.md) |
| `ANNOUNCER_OVERHAUL_PROPOSITION.md` (cited as `OVERHAUL`) | [shipped/announcer-overhaul.md](shipped/announcer-overhaul.md) |
| `AWS_MCP_USAGE.md` | [systems/aws-access.md](systems/aws-access.md) |
| `BRAVE.md` | [systems/search.md](systems/search.md) |
| `FIX_RADIO.md` | [shipped/fix-radio.md](shipped/fix-radio.md) |
| `FIX_RATINGS.md` | [plans/ratings.md](plans/ratings.md) |
| `IDLE.md` | [plans/idle.md](plans/idle.md) |
| `MODELS.md` | [models.md](models.md) |
| `PAYMENT.md` | [shipped/payments.md](shipped/payments.md) |
| `PRIORITIES.md` | [shipped/priorities.md](shipped/priorities.md) |
| `PROMPT_UPGRADE.md` | [shipped/prompt-upgrade.md](shipped/prompt-upgrade.md) |
| `SCALABILITY.md` | [plans/scalability.md](plans/scalability.md) |
| `SFX.md` | [systems/sfx.md](systems/sfx.md) §1–§10 |
| `SFX_PROMPTING.md` | [systems/sfx.md](systems/sfx.md) §11 |
| `SHEEP_PEN.md` | [plans/sheep-pen-brief.md](plans/sheep-pen-brief.md) |
| `SHEEP_PEN_PLAN.md` | [plans/sheep-pen-plan.md](plans/sheep-pen-plan.md) |
| `SONG_GEN_CONCURRENCY_PLAN.md` | [shipped/song-concurrency.md](shipped/song-concurrency.md) |
| `SPLIT_PLAN.md` | [shipped/split.md](shipped/split.md) |
| `TUI.md` | [systems/console.md](systems/console.md) |
| `UI_REFACTOR.md` | [plans/ui-refactor.md](plans/ui-refactor.md) |
| `UI_REFACTOR_FEEDBACK.md` | [plans/ui-feedback.md](plans/ui-feedback.md) |
| `VOTING.md` | [plans/voting.md](plans/voting.md) |
| `future_plans/CGM.md` | [backlog/cgm.md](backlog/cgm.md) |
| `future_plans/FIX_PROMPTING.md` | [backlog/fix-prompting.md](backlog/fix-prompting.md) |

## Reading order for a new agent

1. `RADIO.md` §0 (principles), §3 (architecture), §4 (content types) and §5.2
   (playout).
2. [architecture.md](architecture.md) — the system map and the component list.
3. [backends.md](backends.md) — observed request/response shapes.
4. The active plan you are implementing (`docs/plans/…`).
5. The shipped plan or reference for the area you are touching
   (`docs/shipped/…`, `docs/systems/…`).

## The map

| Path | What it covers | Status |
|------|----------------|--------|
| [architecture.md](architecture.md) | The system map: topology, components, data flow, invariants, glossary | reference |
| [backends.md](backends.md) | Probe records for every external backend (request/response shapes, limits, failure modes) | reference |
| [models.md](models.md) | Which model does what, where it runs | reference |
| [instagram.md](instagram.md) | Posting an aired song to Instagram Reels: the agent runbook (`make reel-post`) | active |
| [systems/sfx.md](systems/sfx.md) | SFX backend (MOSS-SoundEffect v2.0), the two pools, sidecar design, prompt guide (§11) | reference |
| [systems/console.md](systems/console.md) | The Textual DJ console and its admin routes | reference |
| [systems/search.md](systems/search.md) | Web search via the Brave Search API; why searxng was retired | shipped 2026-11-04 |
| [systems/aws-access.md](systems/aws-access.md) | How to read or change the station's AWS account from this checkout | reference |
| [systems/model-fleet.md](systems/model-fleet.md) | The model fleet (twr, spx0/spx1 Sparks, yuri): launcher scripts, /models, `_reset_driver`, systemd drafts | reference |
| [plans/idle.md](plans/idle.md) | Freeze playout and pause production when nobody is listening; wake on PLAY/heartbeat | active |
| [plans/voting.md](plans/voting.md) | Turkey song ratings and request-queue votes | active |
| [plans/ratings.md](plans/ratings.md) | Rating UI amendment to `voting.md`: fractional community average, `avg · n`, grace window | active |
| [plans/ui-refactor.md](plans/ui-refactor.md) | App shell: ABOUT pane, desktop nav, phone tabs + mini player | active |
| [plans/ui-feedback.md](plans/ui-feedback.md) | Living log of operator UI feedback, appended per review round | active |
| [plans/sheep-pen-brief.md](plans/sheep-pen-brief.md) | The Sheep Pen chat brief (read this first) | active |
| [plans/sheep-pen-plan.md](plans/sheep-pen-plan.md) | The Sheep Pen chat implementation plan | active |
| [plans/scalability.md](plans/scalability.md) | The VPC move: Fargate web tier, RDS Postgres, ElastiCache Valkey, Mac as writer | active |
| [../MONITORING.md](../MONITORING.md) | In-process Prometheus metrics: station `:9101`, web tier `:9102`, then an operator handoff | active |
| [shipped/album-art.md](shipped/album-art.md) | Cover art per song (FLUX), S3/CloudFront, Instagram reels | shipped |
| [shipped/payments.md](shipped/payments.md) | Paid priority requests via Stripe Embedded Checkout and webhooks | shipped |
| [shipped/prompt-upgrade.md](shipped/prompt-upgrade.md) | Three-box paid request: lyrics / musical direction / cover art (supersedes the single box in `payments.md`) | shipped 2026-11-08 |
| [shipped/split.md](shipped/split.md) | Splitting the stateful origin from the stateless edge; the edge/gateway/origin layout | complete |
| [shipped/song-concurrency.md](shipped/song-concurrency.md) | Concurrent song generation across the three-deployment pool | shipped |
| [shipped/priorities.md](shipped/priorities.md) | Recycled-song weighting and the persisted airplay ledger | shipped |
| [shipped/fix-radio.md](shipped/fix-radio.md) | Option A: requests splice instead of holding the song rotation | shipped |
| [shipped/announcer-overhaul.md](shipped/announcer-overhaul.md) | Discourse, clock accuracy and self-promotion overhaul | shipped 2026-11-01 |
| [backlog/cgm.md](backlog/cgm.md) | One sound per animal, code-enforced, no re-generation | backlog |
| [backlog/sfx-bug.md](backlog/sfx-bug.md) | SFX bug report (2026-11-10): backend down, no cue retry, weak new-SFX generation | backlog |
| [backlog/fix-prompting.md](backlog/fix-prompting.md) | Song prompting and genre variety | backlog |
| [backlog/monitoring-infra.md](backlog/monitoring-infra.md) | Parked monitoring infrastructure and alerting plan (the previous `MONITORING.md`) | backlog |
| [request-analysis-20261003/report.md](request-analysis-20261003/report.md) | Measured listener-request traffic (arrivals, bursts, latency) | reference |

## Where the code lives

| Area | Files | RADIO.md |
|------|-------|----------|
| Playout | `pilgrim/scheduler.py`, `pilgrim/selector.py` | §5 |
| Production | `pilgrim/producer.py`, `pilgrim/pipelines/` | §6 |
| Station bible / running gags | `pilgrim/bible/`, `pilgrim/promotions.py` | §7 |
| Audio QC, normalization, MP3 | `pilgrim/audio/` | §8 |
| Client | `pilgrim/web/` | §9 |
| Voices | `pilgrim/config.yaml` `voices`, `pilgrim/prompts/` | §10 |
| Server | `pilgrim/server.py` | §11 |
| Config | `pilgrim/config.yaml`, `pilgrim/config.py` | §12 |
| Storage | `pilgrim/store.py`, `pilgrim/db.py`, `pilgrim/hotstate.py` | §14 |
| Media publishing | `pilgrim/media_publish.py`, `pilgrim/album_art.py` | §8.4 |
| Paid requests | `pilgrim/payments.py` | — (`shipped/payments.md`) |
| DJ console | `pilgrim/tui/` | — (`systems/console.md`) |
| Sheep Pen chat | `pilgrim/chat/`, `pilgrim/chat_worker.py` | §11, §14 |
| Web tier | `pilgrim/web_app.py` | §11 "Web tier" |
| Deploy | `pilgrim/deploy/` (see `pilgrim/deploy/README.md`, `RUNBOOK.md`) | §17 |

## Maintenance

- Add a new plan under `docs/plans/`, one line to `AGENTS.md`'s active-plans
  list, and a row to the map above.
- Do not delete shipped plans; move them and add a status note.
- Keep `docs/backends.md` factual: probe first, then write down what you saw.
