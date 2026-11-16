# PENDING.md — planned, not built

What the station does today is in `RADIO.md`. This file lists what has been
planned and is **not** in the code or not yet live. Nothing here is scheduled
until the operator says so; an agent does not start an item from this list on
its own.

When an item ships, delete it here, put the behaviour in `RADIO.md`, and leave
a short record in `docs/shipped/`.

| # | Item | State | Detail |
|---|------|-------|--------|
| 1 | Station memory and the public wiki | written, not started, **parked** | `docs/pending/agent-wiki.md` |
| 2 | Finish the VPC move | Steps 6, 7 and the end of 9 open | `docs/pending/scalability.md` |
| 3 | Monitoring: web-tier scrape, dashboard, alerts | operator steps open | `docs/shipped/monitoring.md` §5, `docs/pending/monitoring-infra.md` |
| 4 | Sheep Pen: release tail | operator steps open | `docs/shipped/sheep-pen.md` |
| 5 | SFX: one sound per animal, retry failed cues | diagnosed, not scheduled | `docs/pending/cgm.md`, `docs/pending/sfx-bug.md` |
| 6 | Song prompting and genre variety | diagnosed, not scheduled | `docs/pending/fix-prompting.md` |
| 7 | Console: skip the current segment | designed, not built | `docs/systems/console.md` §5, §7 |
| 8 | Rating row greys out mid-song | known issue | below |
| 9 | Ratings weight the rotation | built, switched off | below |
| 10 | Roadmap items from the original spec | ideas | below |

---

## 1. Station memory and the public wiki — parked

Short-term memory in DJ talk, wiki pages the writers read, a keeper that
writes pages, and a publisher that pushes them to a GitHub wiki repository.
The plan is complete and written for the local agent (eight steps with exact
code). No step has been run and no `wiki` code exists. **The operator has
parked it (2026-11-16); do not start it.**

Before it is picked up again, check its code blocks against the tree: it was
written against `pilgrim/producer.py` and `pilgrim/store.py` as they were on
2026-11-15. Both are packages now (`pilgrim/producer/`, `pilgrim/store/`)
with the same classes and method names, one module per content type or group
of tables, so the plan's "in this file" instructions need new file names.

## 2. Finish the VPC move

Done: the web image and Fargate service (Step 1), the Postgres store and the
production cutover (Step 2), hot state in Valkey with the asynchronous wake
(Steps 3–4), the web entry point `pilgrim/web_app.py` as the public face
(Step 5), the bastion route and the rest of the infrastructure (Step 8).

Open, by the plan's step numbers:

- **Step 6 — no origin media fallback.** `player.js` still retries a stalled
  CloudFront fetch from `/api/media/<id>`, which the web tier cannot serve.
  The retry should go back to CloudFront, and the emergency pack should be
  uploaded at seed.
- **Step 7 — the station as a writer only.** `server.py` still builds the
  public listener on the Mac. It should keep the loopback admin listener and
  drop the public one.
- **Step 9 — retire the old edge.** `pilgrim/deploy/edge`, `gateway`,
  `interim`, the `make edge-*` targets and Tailscale serve on the M5 are
  still in the repo. `pilgrim/deploy/README.md` "Current production" and the
  Edge row of AGENTS §2 still describe the EC2 edge as live.
- **Follow-up:** the request-line and vote rate limiters are per task. Move
  them into Valkey before the service runs more than one task.

## 3. Monitoring

The code is shipped and the origin is scraped (`pg-origin` is up). Open, all
on the operator's side (`docs/shipped/monitoring.md` §5.0, §5.4, §5.6):

- **`pg-web` has no targets.** The NAS has no `[kenny-logger]` AWS profile, so
  ECS discovery finds nothing. Add the profile and restart `prom_serv`.
- **`lilnas` is untagged** on the tailnet, so the `tag:monitoring` grant for
  port 9102 cannot match it.
- **The Station dashboard** in Grafana does not exist.
- **Alerting** is out of that plan. The catalogue to cut down is
  `docs/pending/monitoring-infra.md` §4; the rows that use `pilgrim_*` series
  are #4, #7, #10–12, #15–17, #24–25 and #31–33. The same file holds the
  parked whole-stack design (second Prometheus, yace, blackbox probes, host
  and GPU exporters, canary, budgets, agent responder).
- There are no metrics for image generation (FLUX).

## 4. Sheep Pen release tail

The chat is built and deployed. From Steps 11–12 of the plan, confirm or do:

- Terraform: the chat audit log group (`/aws/pilgrim/chat-audit`, name to be
  confirmed), `chat_log_retention_days`, and the scoped `logs:` statement on
  the task role.
- Live acceptance against https://pilgrim.farm with the review worker and the
  admin controls verified.
- Open question: chat admin actions have HTTP routes
  (`/api/admin/chat/*`) and no screen in the console.
- Sizing `ecpu_per_second.maximum` for Valkey from the measured per-listener
  command rate, when the service scales.

## 5. SFX

Two documents on one subsystem, neither scheduled:

- `docs/pending/cgm.md` (2026-10-02): one fixed sound per farm animal,
  enforced in code, with no re-generation, so a mentioned animal is always
  heard.
- `docs/pending/sfx-bug.md` (2026-11-10): the backend is often down, failed
  cues are not retried, and newly generated effects are weak. The operator's
  call: SFX is not mission critical.

## 6. Song prompting and genre variety

`docs/pending/fix-prompting.md`: a diagnosis of why briefs converge on a few
styles, and an implementation plan against the MiniMax Music 3 prompting
guide. Check it against `pilgrim/genres.py` and `prompts/song_brief.md`
before use; both have changed since it was written.

## 7. Console: skip the current segment

`docs/systems/console.md` §5 and §7 design skip and queue commands for the
operator console. "Play this song next" (`POST /api/admin/dj/play`) and the
phrase jobs are built; skip is not, and the DJ persistence tables in RADIO
§14 are design only.

## 8. Rating row greys out mid-song

Recorded as a known issue in RADIO §9.1. The turkey row can grey out while a
song is audible when the state poll's `current_seq` is ahead of the client's
audio clock. Intended fix: keep the row open until `pd:nowplaying` moves off
the song, and let a late click take the server's 409.

## 9. Ratings weight the rotation

`ratings.rotation_weight` exists and is `false` (docs/shipped/voting.md §8).
Turning it on lets song ratings weight the recycled-song draw. The operator
wanted a week of ratings first.

Other defaults still open from the shipped plans, each with a default in
force: pruning for `request_votes` and `song_ratings` (they store the
canonical IP, like `visitor_ips`); whether a long-buried request is evicted;
`ratings.max_voters_per_ip` (3); `idle.after_s` (120).

## 10. Roadmap items from the original spec

Carried over from RADIO §17 as written. They have not been reviewed against
what has been built since, so some may be done in another form.

- `ClockSelector`: an hour clock with hard news posts at :00/:30 and
  backtiming (fill to the post with liner length buckets). News starts at
  :45/:15 and is ready by :55/:25; if late, re-air the previous bulletin with
  a recap intro.
- Music beds mixed under voice spots, offline at render time.
- A continuity step that proposes station bible additions. (Item 1 covers
  much of this.)
- A larger news model on the 5090.
- A multi-listener wall-clock timeline with drift reconciliation.
- Persistent station state, crash recovery, resume.
- A library pruning policy.
- True streaming transport, only if sequential playback proves limiting.
