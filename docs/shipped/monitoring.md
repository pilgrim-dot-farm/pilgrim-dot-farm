# Monitoring — in-process application metrics

> *Formerly `MONITORING.md` at the repo root.*

**Status: code shipped** (Steps 1–6, 2026-11-14); the origin is scraped. The
operator items still open (the `pg-web` scrape and the Station dashboard) are
in PENDING.md, with the detail in §5 below.

The station and the web tier each fill an in-process registry and serve
`GET /metrics` on their own port (`9101` on the Mac, `9102` in the container)
for the Prometheus on the operator's NAS. Normative behaviour: RADIO §11
("Metrics"), §12 (`metrics:`); AGENTS §10 (the `/metrics` pitfall).

Code: `pilgrim/metrics.py` (registry, text format, log-event handler, server),
`pilgrim/station_metrics.py`, `pilgrim/web_metrics.py`,
`pilgrim/logging_setup.py`. The earlier whole-stack plan is
docs/pending/monitoring-infra.md.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:MONITORING.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.2 Decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | Metrics are **in-process**: the station and the web tier each fill a registry and serve `GET /metrics`. No sidecar exporter, no log tailing | operator, 2026-11-11 |
| D2 | No new dependency. `pilgrim/metrics.py` is a ~200-line registry + text format + stdlib `http.server` | AGENTS §3 |
| D3 | `/metrics` is on **its own port**: `9101` on the Mac, `9102` in the container. Not on the public, admin or ALB port | admin must stay loopback; the ALB target group only knows 8080 |
| D4 | The metrics server and loop start **only** from `server.main()` and `web_app.main()`. `build_station`, `Station.startup`, `create_app`, `create_web_app` are unchanged, so tests, `make sim` and the e2e servers never open the port | AGENTS §1.2 |
| D5 | Log events become counters through a `logging.Handler` on the root logger. Metrics code never changes playout behaviour; the only edits to `scheduler.py`, `producer.py` and the pipelines are **log-message renames** (Step 5) that give the non-dotted warnings a dotted event name | AGENTS §1.1, §1.3, §1.11 |
| D6 | Station gauges come from two cadences on the event loop: in-memory values every `metrics.live_s` (5 s), `await station.health()` every `metrics.refresh_s` (60 s). A scrape does no I/O: it renders the registry | playout never waits on monitoring |
| D7 | Metric prefix `pilgrim_`. Web-tier-only families are `pilgrim_web_*`. The Prometheus `job` label (`pg-origin`, `pg-web`) tells the tiers apart for shared families; `job` keeps the short infra form because it mirrors `pg-app`/`pg-db`/`pg-cache` | operator, 2026-11-13: `pg_` collides with `postgres_exporter` (`pg_up`, `pg_stat_*`, `pg_settings_*`) if RDS is ever scraped |
| D8 | The web tier is found by Prometheus `aws_sd_configs` `role: ecs`, kept by the service tag `prometheus_scrape=true`, scraped over the bastion's tailnet route | operator, 2026-11-11 |
| D9 | Alerting, probes from outside, and AWS-side failsafes are **not** in this plan | parked in `docs/pending/monitoring-infra.md` |

### 0.3 Non-goals

- No change to any HTTP route in RADIO §11's table. `/metrics` is a new socket,
  documented in a new §11 "Metrics" paragraph.
- No Grafana dashboard JSON in the repo. The operator (or Claude through the
  Grafana MCP) builds the **Station** dashboard after Step 4 ships (§5.6).
- No alert rules. No blackbox or host exporters. No CloudWatch changes.
- No metrics socket for the chat worker process (`pilgrim/chat_worker.py`)
  itself. The web tier reports its liveness from the `worker:heartbeat` key the
  worker already writes to Valkey (§2.3), so a dead worker is visible without a
  third process to scrape.
- The local agent does **not** apply terraform, edit the live tailnet policy,
  touch the NAS, build images or restart the station. Those are §5, operator.

## 1. Shape

```
 NAS (LAN, tag:monitoring)                     M5 (origin)
 ┌────────────────────────┐   LAN :9101   ┌───────────────────────────────┐
 │ Prometheus             │──────────────▶│ python -m pilgrim.server      │
 │  job pg-origin         │               │  metrics thread: GET /metrics │
 │  job pg-web (ECS SD)   │               │  metrics task: live 5 s,      │
 │ Grafana                │               │   health 60 s  → registry     │
 └───────────┬────────────┘               │  LogMetrics handler → registry│
             │ tailnet, tcp:9102 only     └───────────────────────────────┘
             ▼
 bastion pf2 (subnet router, SNAT) ──▶ Fargate task private IP :9102
                                       python -m pilgrim.web_app
                                        request middleware, cache view,
                                        chat sockets, LogMetrics
```

One registry per process. Writers: the event loop (the metrics task, the web
middleware) and the log handler (any thread). Reader: the metrics server
thread. Every access takes one lock.

---

## 2. Metric catalogue

No label ever carries a player id, voter id, IP, email, token, title, URL,
request id or chat text (§6 rule 3; `metrics.FORBIDDEN_LABELS` enforces it).

### 2.1 Both tiers

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `pilgrim_log_events_total` | counter | `logger`, `event`, `level` | every log record whose message starts with a dotted event name; non-event records only at WARNING and above, as `event="other"` |
| `pilgrim_llm_call_seconds` | histogram | `model`, `status` | `llm.call` records (`duration_ms`) |
| `pilgrim_process_start_timestamp_seconds` | gauge | | set at start |
| `pilgrim_metrics_refresh_timestamp_seconds`, `pilgrim_metrics_refresh_failures_total` | gauge / counter | | the metrics loop about itself |

`pilgrim_log_events_total` is the workhorse. Examples the dashboard will use:
`program.starved`, `program.low_coverage`, `song.produced`, `song.rejected`,
`song.backend.down`, `sfx.failed`, `request.received`, `request.failed`,
`request.aired`, `vote.cast`, `rating.cast`, `payment.applied`,
`payment.refund_due`, `moderation.decision`, `hotstate.down`, `station.wake`,
`station.idle`, `media.published`, `chat.*`, `web.*`.

`event="other"` is the escape hatch: a WARNING-or-above record whose message
is not a dotted event. Step 5 renames the twenty such lines in the station so
that only third-party noise (uvicorn, redis, the AWS SDK) lands in `other`.

### 2.2 Origin only (`job="pg-origin"`)

| Metric | Type | Labels | Cadence |
|--------|------|--------|---------|
| `pilgrim_on_air`, `pilgrim_idle`, `pilgrim_listeners` | gauge | | live |
| `pilgrim_committed_coverage_seconds`, `pilgrim_committed_items`, `pilgrim_program_revision` | gauge | | live |
| `pilgrim_current_type` | gauge (1) | `type` | live |
| `pilgrim_request_stage` | gauge | `stage` (`waiting`, `writing`, `generating`, `rendered`) | live |
| `pilgrim_starved` | gauge | | live: 1 when coverage is zero and the station is not frozen (dead air, not idle) |
| `pilgrim_last_starve_timestamp_seconds`, `pilgrim_starve_gap_seconds` | gauge / histogram | | a `program.starved` record (`created` time, `gap_s`) |
| `pilgrim_idle_since_timestamp_seconds`, `pilgrim_idle_wakes_total` | gauge / counter | | health |
| `pilgrim_listeners_reporting`, `pilgrim_listeners_hidden`, `pilgrim_listeners_not_running`, `pilgrim_listeners_sink_element`, `pilgrim_listeners_resumed`, `pilgrim_listeners_by_platform` | gauge | `platform` on the last | health. Read from the `player:<id>` keys (one GET per live id), because the web tier handles every heartbeat in the split deployment |
| `pilgrim_inventory_have`, `pilgrim_inventory_target`, `pilgrim_rotation` | gauge | `type` | health |
| `pilgrim_backend_up` | gauge | `backend` (`litellm`, `kokoro`, `music`, `search`, `sfx`) | health |
| `pilgrim_music_backend_up`, `pilgrim_music_slots` | gauge | `name` | health |
| `pilgrim_music_deployments`, `pilgrim_music_deployments_listed` | gauge | `name` | health; the healthy / listed deployments behind a `slots: auto` backend. `music_slots` floors at 1, so only this pair shows "no box behind the proxy is up" |
| `pilgrim_music_busy`, `pilgrim_music_capacity` | gauge | `name` on the first | live; renders in flight (the pool's leases) and slots usable now. The mlx-serve music endpoint does not feed its own `vllm:num_requests_*` gauges, so this is the only accurate "a render is running" signal (2026-11-15) |
| `pilgrim_search_last_timestamp_seconds` | gauge | | health (Brave is never probed; `pilgrim_backend_up{backend="search"}` is the health signal) |
| `pilgrim_song_realtime_ratio` | gauge | | health |
| `pilgrim_art_backend` (1), `pilgrim_art_items` | gauge | `state` / `with_art` | health |
| `pilgrim_db_engine` (1) | gauge | `engine` | health |
| `pilgrim_cache_enabled`, `pilgrim_cache_healthy`, `pilgrim_cache_ticks_total`, `pilgrim_cache_failures_total`, `pilgrim_cache_last_publish_timestamp_seconds`, `pilgrim_cache_pending_players` | gauge / counter | | health |
| `pilgrim_song_render_seconds`, `pilgrim_song_render_rtf` | histogram | `generator` | `song.produced` records that carry `wall_s` |
| `pilgrim_reel_ledgers`, `pilgrim_reel_failed_ledgers`, `pilgrim_reel_last_posted_timestamp_seconds`, `pilgrim_reel_quota_usage`, `pilgrim_reel_quota_total` | gauge | `status` / `stage` | health; read from `pilgrim/logs/reels/*.json`, never the Graph API |
| `pilgrim_metrics_refresh_duration_seconds` | gauge | | health |

### 2.3 Web tier only (`job="pg-web"`)

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `pilgrim_web_http_requests_total` | counter | `route`, `method`, `status` | middleware; `route` is the template (`/api/requests/{request_id}/vote`), `static` for everything outside `/api/` |
| `pilgrim_web_http_request_seconds` | histogram | `route` | middleware |
| `pilgrim_web_cache_up`, `pilgrim_web_station_on_air` | gauge | | one Valkey read of the `health` document every `live_s` |
| `pilgrim_web_chat_sockets` | gauge | | `len(chat_hub.sockets)` |
| `pilgrim_web_chat_worker_up`, `pilgrim_web_chat_worker_heartbeat_timestamp_seconds` | gauge | | one Valkey read of `worker:heartbeat` every `live_s`; 0 when chat is disabled or the worker is down |

## 5. Operator handoff (not the agent)

### 5.0 Status 2026-11-14

- **Origin: done.** The station was restarted; `GET :9101/metrics` serves 49
  families / 97 samples, `pilgrim_on_air 1`, `pilgrim_starved 0`, and
  `/metrics` 404s every other path. Origin-only histograms are present; no
  `pilgrim_web_*` family appears.
- **Web image: done.** `make image-smoke` PASS; the web image was built
  (`pilgrim:web`, digest `sha256:1dba6575…`) and pushed as
  `pilgrimdotfarm/front-end:5530a17` and `:latest`.
- **Terraform: applied.** Release 27, task definition `pg-app:23`
  (1/1 ACTIVE): ports 8080 + 9102, `service_tags.prometheus_scrape=true`,
  the `pg-ec2` SG 9102 self-ingress rule, and `kenny-logger`'s
  `ecs-discovery-read` are all live. Container log:
  `metrics.ready host=0.0.0.0 port=9102`; `https://pilgrim.farm/metrics` → 404.
- **NAS Prometheus: partly done.** `prom_serv` (Prometheus 3.11.3, Docker on
  `lilnas`) now has both jobs. **`pg-origin` is UP** (`up{job="pg-origin"}=1`,
  `pilgrim_on_air=1` scraped from `192.168.68.90:9101`). **`pg-web` is
  configured but has no targets**: the NAS's `~/.aws/credentials` has only an
  old-account `[default]`; there is no `[kenny-logger]` profile. Add one (the
  active key created 2026-11-07) and `docker restart prom_serv` — lifecycle
  reload is disabled (403). `~/.aws` is mounted read-only at `/root/.aws`;
  backups: `prometheus.yml.bak-20261114`, `docker-compose.yml.bak-20261114`.
- **Not done:** the Station dashboard (§5.6).

### 5.3 Verify, before touching Prometheus

```sh
# on the Mac
curl -s 127.0.0.1:9101/metrics | grep -c '^pilgrim_'            # > 50
curl -s 127.0.0.1:9101/metrics | grep '^pilgrim_on_air'
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:9101/api/health   # 404

# from the bastion (inside the VPC); TASK_IP from the ECS console
ssh pf2 "curl -s http://TASK_IP:9102/metrics | grep '^pilgrim_web_'"

# the ALB must NOT serve it
curl -s -o /dev/null -w '%{http_code}\n' https://pilgrim.farm/metrics  # 404 or the site's fallback, never metrics text

# from the NAS (tailnet route + grant + security group)
curl -s http://TASK_IP:9102/metrics | head -3

# on the LAN, from the NAS itself
curl -s http://192.168.68.90:9101/metrics | grep -c '^pilgrim_'      # > 50
```

If Prometheus runs in a container on the NAS, run the last check from inside
that container — and note that a bridge-network container does **not** inherit
the host's tailnet route to `172.31.0.0/16`. Use `network_mode: host` (the NAS
is Linux) or add the route, otherwise `pg-web` tasks are unreachable even
though the tailnet grant and SG allow them.

### 5.4 Prometheus on the NAS

`kenny-logger`'s key goes in the Prometheus user's `~/.aws/credentials` as a
profile (or in its environment). Then:

```yaml
  - job_name: pg-origin
    static_configs:
      - targets: ["192.168.68.90:9101"]

  - job_name: pg-web
    aws_sd_configs:
      - role: ecs
        region: us-west-2
        profile: kenny-logger
        clusters: ["arn:aws:ecs:us-west-2:477688903218:cluster/pg-ecs"]
        port: 9102
    relabel_configs:
      - source_labels: [__meta_ecs_tag_service_prometheus_scrape]
        regex: "true"
        action: keep
      - source_labels: [__meta_ecs_last_status]
        regex: RUNNING
        action: keep
      - source_labels: [__meta_ecs_task_arn]
        regex: ".*/([^/]+)$"
        target_label: task
```

On `lilnas`, Prometheus is the Docker container `prom_serv`
(`ubuntu/prometheus:3.11-26.04_stable`, Prometheus **3.11.3** — `role: ecs` is
supported; §8 Q4 closed). Config:
`~/compose_stack/prometheus/prometheus.yml`; compose:
`~/compose_stack/docker-compose.yml`; `~/.aws` is mounted read-only
at `/root/.aws`. Lifecycle reload is **disabled** (`POST /-/reload` → 403), so
a config change needs `docker restart prom_serv` (or
`docker compose -p compose_stack up -d prom_serv` after a compose edit).
Backups from 2026-11-14: `prometheus.yml.bak-20261114`,
`docker-compose.yml.bak-20261114`.

Credentials: `pg-origin` needs none. `pg-web` needs a `[kenny-logger]` profile
in `~/.aws/credentials` — the NAS's `[default]` is an old-account key, so the
job currently has no targets. Add the profile (the active key created
2026-11-07), `docker restart prom_serv`, then `up{job="pg-web"}` should be 1.

Done when `up{job="pg-origin"} == 1` and `up{job="pg-web"} == 1`.

### 5.5 What scraping costs the station

Nothing on the playout path. The 60 s refresh makes the same backend `GET`s
and inventory counts the admin `/api/health` makes on demand (≈7 probes/min,
not the ~17k/day a 30 s cadence would cost). If that ever shows up as load,
raise `metrics.refresh_s` again; the live gauges are unaffected.

### 5.6 Station dashboard

A third dashboard in the **Pilgrim Dot Farm** folder, built once real series
exist. Suggested rows: **dead air** (`pilgrim_starved`, `pilgrim_last_starve_timestamp_seconds`,
`pilgrim_starve_gap_seconds`, `rate(pilgrim_log_events_total{event="program.starved"}[5m])`),
on air / idle / listeners / coverage; inventory vs target and rotation;
backends and music slots; requests (stage gauge, event rates); renders
(`pilgrim_song_render_seconds`, `pilgrim_song_render_rtf`, LLM latency);
hot-state publisher; web tier (requests by route and status, p95 latency,
cache view, chat sockets, `pilgrim_web_chat_worker_up`); log events by level;
Reels. Sum `pilgrim_web_*` across tasks (`sum by (job)`) during a rolling deploy.

---

## 6. Hard rules for this plan

In addition to AGENTS §1:

1. Metrics code never calls `GET /api/station/program` or `POST
   /api/station/heartbeat`, never probes Brave, FLUX or the Meta Graph API,
   never asks a model for anything. The only station call is
   `Station.health()`, in-process.
2. `pilgrim/metrics.py`, `pilgrim/station_metrics.py` and
   `pilgrim/web_metrics.py` never import `scheduler`, `producer`, `selector`,
   `payments`, `store` or `db` (a test enforces it). They read the station
   object they are handed.
3. No label carries a player id, voter id, user id, IP, email, token, title,
   URL, request id, item id or text. Label values are config-bounded names
   only (types, backends, models, generators, route templates, event names).
4. `/metrics` is never mounted on the public, admin or ALB-facing app, and is
   never reachable through Cloudflare.
5. A metrics failure is logged and counted. It never raises into the station
   or the web app, and a taken port means "no metrics", not "no station".
6. Scrape handlers do no I/O beyond rendering the registry.
7. A metrics tick never mutates station state. It reads through read-only
   accessors (`Station.listener_count()`, `scheduler.coverage()`), never the
   pruning ones (`Station.active_listeners()`), so monitoring cannot move the
   idle decision.

## 8. Open questions (operator)

1. **`pf` vs `pf2`.** `~/.ssh/config` has `pf` → `52.53.93.0` (retired account)
   and `pf2` → `52.13.70.63` (the bastion). This plan says `pf2`. Repoint `pf`
   if you want that name to mean the bastion.
2. **The M5's LAN address — resolved 2026-11-14.** The Mac Studio's only LAN
   interface is `en1` = **`192.168.68.90`** (the same address Prometheus
   scrapes mlx-serve at); the parked plan's `192.168.68.51` is stale. Verified
   `curl http://192.168.68.90:9101/metrics` → 121 `pilgrim_*` samples and
   `/api/health` → 404.
3. **`metrics.host: "0.0.0.0"` on the Mac.** It exposes `:9101` (read-only, no
   private labels) to the home LAN, like mlx-serve's `:11234`. The tailnet
   policy has no grant for it. Say so if you would rather bind the LAN address.
4. **Prometheus version on the NAS** for `aws_sd_configs` `role: ecs` (§5.4).
5. **Alerting.** Out of scope here. Once the series exist, the catalogue in
   `docs/pending/monitoring-infra.md` §4 can be cut down to the rows that use
   `pilgrim_*` (#4, #7, #10–12, #15–17, #24–25, #31–33) as Grafana-managed alerts.

### 8.1 Resolved 2026-11-13

| # | Question | Answer |
|---|----------|--------|
| 1 | `pg_` prefix vs `postgres_exporter` | **`pilgrim_`** for every metric name; `job` labels stay `pg-origin`/`pg-web` (D7) |
| 2 | Non-dotted WARNING+ lines collapse to `other` | **Fixed** by renaming the twenty lines to dotted events (Step 5; D5 narrowed to log-message renames) |
| 3 | `MAX_EVENTS = 400` | **`9000`** (≈226 events exist today; the cap is a safety valve) |
| 4 | Redundant metrics | **Dropped** `pilgrim_search_last_ok`; `pilgrim_backend_up{backend="search"}` is the signal |
| 5 | `refresh_s = 30` | **`60`** |
| 6 | Dead air | **Added** `pilgrim_starved`, `pilgrim_last_starve_timestamp_seconds`, `pilgrim_starve_gap_seconds` |
| 7 | Origin-only histograms on the web tier | **`LogMetrics(station_histograms=False)`** for the web tier |
| 8 | `live()` mutating listener state | **`Station.listener_count()`** (read-only twin of `active_listeners()`); §6 rule 7 |
| 9 | IAM scope for ECS discovery | **Verified 2026-11-14: not present** — `kenny-logger` has only `logs:*`/`cloudwatch:*`; add the policy (§5.1) |
| 10 | Chat worker invisible | **Covered cheaply**: the web tier reads the worker's existing `worker:heartbeat` key → `pilgrim_web_chat_worker_up` (§2.3) |

---

## 9. Parked

`docs/pending/monitoring-infra.md` is the previous version of this file,
verbatim under a short header: the two-Prometheus design, yace, blackbox
probes, host and GPU exporters, CloudWatch failsafe alarms, the Synthetics
canary, budgets, the alert catalogue and the agent responder. Its §1 survey
(2026-11-07) is still the best host inventory. Nothing there is scheduled.
