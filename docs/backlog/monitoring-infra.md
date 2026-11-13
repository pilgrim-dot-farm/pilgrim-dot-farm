> **Parked 2026-11-11.** This is the previous `MONITORING.md`, verbatim. It planned a full
> monitoring stack; Prometheus and Grafana already exist on the operator's NAS, and the
> active plan (`MONITORING.md` at the repo root) covers only in-process application
> metrics. Nothing below is scheduled. Its §1 survey and §4 alert catalogue remain useful
> reference. Section numbers cited elsewhere as `MONITORING §n` before 2026-11-11 refer to
> this file.

# MONITORING.md — Prometheus + Grafana for the farm, cheap AWS metrics where they are free

Status: plan, 2026-11-07. Nothing in this file is built. The survey in §1 is
real (probed from the M5 and over SSH on 2026-11-07, read-only). Alerting is
designed here (§4, §5) but implemented last (Steps 9–10): the catalogue and the
escalation path are decided now so the metrics collected in Steps 1–8 are the
ones the alerts will need. RADIO.md stays the source of truth; the only RADIO
section this plan touches is §11, and only in the optional Step 11.

Read first: `AGENTS.md` (§1 hard rules, §10 pitfalls on waking the station),
RADIO.md §2, §3, §11 (`/api/health` on the admin listener), §14 (hot state),
`SCALABILITY.md` §0–§1, `pilgrim/deploy/README.md` ("Current production",
"Bastion subnet router"), `pilgrim/deploy/bastion/README.md`. Cite this plan as
`MONITORING §n`.

**Coordination note.** Another agent is implementing the Sheep Pen chat in this
checkout (SHEEP_PEN_PLAN.md; `server.py`, `web_app.py`, `config.py`,
`config.yaml`, `logging_setup.py`, the container overlay and `docs/backends.md`
are theirs right now). Every step below is additive and lands in **new files
only** (`pilgrim/monitoring/`, `pilgrim/deploy/monitoring/`, `docs/monitoring.md`,
new tests) until Step 11, which waits for the chat work to merge. Do not edit
the files above, do not restart the station, do not re-render the launchd units
the station uses.

---

## 0. Target

```
                     ┌──────────────── home LAN ────────────────────────────────┐
                     │ M5 (origin)                                              │
   Grafana :3000 ◀───┤  Prometheus :9090  ── scrapes ──▶ station exporter :9101 │
   (tailnet only)    │  Alertmanager :9093              node_exporter :9100     │
                     │  blackbox_exporter :9115 ──▶ kokoro, sfx, audiocpp×2,     │
                     │                              mlx-serve, LiteLLM, gateway, │
                     │                              Valkey/RDS tcp via the route │
                     │     ── scrapes ──▶ twr (5090), spx0, spx1 (Sparks), .89 (Kokoro box): │
                     │                   node_exporter :9100, nvidia_gpu_exporter :9835      │
                     └────────────┬─────────────────────────────────────────────┘
                                  │ tailnet (both ways, tcp:9090)
                     ┌────────────┴─────────────────────────────────────────────┐
                     │ bastion pf2 (AL2023, in the VPC)                         │
                     │  Prometheus :9090 (tailnet ip only) ── scrapes ──▶        │
                     │    blackbox :9115 ──▶ https://pilgrim.farm/api/health,    │
                     │                      ALB direct, CloudFront object,      │
                     │                      relay :4000, Valkey/RDS from inside │
                     │    node_exporter :9100, textfile: tailscale route        │
                     │    yace :5000 ◀── CloudWatch GetMetricData (instance role)│
                     │    the M5's Prometheus /-/healthy  (watchdog, §4 P1)     │
                     └──────────────────────────────────────────────────────────┘
   CloudWatch (free metrics: ALB, target group, ECS service, RDS basic,
   ElastiCache serverless, CloudFront, S3; EMF from the chat worker) ──▶ yace
   CloudWatch alarms (a handful, §5 Step 7) ──▶ SNS ──▶ operator: the failsafe
   when both Prometheus are down
```

Two Prometheus, one Grafana. The M5 is the LAN's truth: it alone can reach the
model boxes and the station's admin listener. The bastion is the outside-in
vantage point: it sees pilgrim.farm the way a listener does, it sees the VPC
from the inside, and it is the only thing that can notice the M5 (or the home
internet) is gone. Grafana on the M5 reads both. Nothing listens on a public
address; the tailnet carries every cross-site scrape.

---

## 1. What exists today (survey, 2026-11-07)

Fleet note (2026-11-10): the 2x-Spark DeepSeek job was stopped (operator
instruction, to vet ninfer on spx1); both Sparks are since clean and idle.
The model boxes' launcher scripts, the `_reset_driver` procedure and the
systemd story live in `docs/systems/model-fleet.md`.

### 1.1 Hosts

| Host | Address | Role | Already exposes | Notes |
|------|---------|------|-----------------|-------|
| M5 Mac Studio | `192.168.68.51`, tailnet `100.114.40.70` | station (launchd `farm.pilgrim.station`, :5080 public, :5081 admin), gateway nginx :8090, `tailscale serve :443`, **mlx-serve :11234** (MiniMax, bare process, up 17 h), **FLUX :8084** (bare process, up 5 d) | nothing for Prometheus. `mlx-serve /metrics` → 503. Admin `/api/health` is the richest station signal (§1.3) | macOS 27.0.1, 64 GB, 547 GB free. No Homebrew prometheus/grafana/node_exporter installed. mlx-serve and FLUX are **not** under launchd: nothing restarts them |
| twr (5090 box) | `192.168.68.75` | LiteLLM (docker :4000, behind `litellm.twr.church`), ollama, ninfer-serve :8081, Kokoro test container, searxng, n8n, ragflow, minio, mysql, es, valkey… | **node_exporter :9100 reachable** (container `prom_export`) | Linux Mint 22.1, RTX 5090 32 GB, 124 GB RAM, **disk 94 % full (109 GB free)**. `GET /metrics` on LiteLLM answers **307** with a token (unverified: LiteLLM gates Prometheus behind enterprise in some versions; `/health/liveliness` → 200) |
| spx0 (DGX Spark) | `192.168.68.88` | per `docs/backends.md` §2b: audiocpp MiniMax `:8080` and MOSS SFX `:8000` | node_exporter listens on `*:9100` but is **not reachable from the M5** (firewall?) | Ubuntu 24.04, GB10 (nvidia-smi reports memory `[N/A]`: unified memory), **disk 96 % full**. Right now runs `dsv41-1017-exl3-tf` (a DeepSeek exl3 tensor-parallel job across both Sparks over `192.168.100.x`); **`:8080` and `:8000` are not listening** → the station shows `sfx: false`, `music_slots: {litellm: 1}` |
| spx1 (DGX Spark) | `192.168.68.56` | audiocpp MiniMax `:8080`; also `Qwen3-Omni` api_base | **node_exporter :9100 reachable** | Ubuntu 24.04, GB10, **disk 99 % full (71 GB free)**. `:8080` not listening (same job as spx0). `:8000` is an ssh tunnel (`vllm-fn-tunnel.service`) |
| Kokoro box | `192.168.68.89` (MAC `78:20:51:…`) | Kokoro TTS `:8001` (`/health` → 200) | **node_exporter :9100 reachable** | Not in `~/.ssh/config` by this address; probably `yuri` (the 4070 Ti box; `yuri.local` does not resolve). **Confirm identity and SSH access in Step 1** |
| mini | `192.168.68.63` | unknown | — | SSH timed out; not part of the station. Ignore unless the operator says otherwise |
| bastion pf2 | `172.31.35.80`, public `52.13.70.63`, tailnet `100.66.128.118` | Tailscale subnet router (`172.31.0.0/16`), LiteLLM relay (`pilgrim-litellm-relay`, nginx, host :4000, `/relay-health` → 200), SSM agent | nothing: no node_exporter, no CloudWatch agent | Amazon Linux 2023, c7i-flex.large, 3.8 GB RAM, 29 GB free. Instance profile `pg-s3-read` (S3 read, secret read, `logs:*`) |
| Fargate `pg-app` | private subnets, behind `pg-alb` | web tier (`pilgrimdotfarm/front-end`), 1 task desired/running, task def `pg-app:6` | container health check + ALB health check on `/api/health`; stdout → `/aws/ecs/pg-app/app` (7 d) | Container Insights is **enabled** on `pg-ecs` (performance log group, 1-day retention). No `/metrics` endpoint |
| RDS `pg-db` | `pg-db.chg6qwekyier.us-west-2.rds.amazonaws.com` | Postgres 18.6, db.t4g.micro, 20 GB gp3 | free basic CloudWatch metrics (1-min) | Enhanced monitoring off, Performance Insights off (keep both off: cost), 1-day backups, single-AZ |
| ElastiCache `pg-cache` | serverless Valkey 9 | hot state + chat | free CloudWatch metrics | caps: 5 GB, 1000 ECPU/s |
| ALB `pg-alb` + TG `pg-http` | `app/pg-alb/236b01bde30ec6af`, `targetgroup/pg-http/5bb66ec3ec8c18c0` | public entry behind Cloudflare (`pilgrim.farm` → `104.21.79.233`, `172.67.149.236`) | free CloudWatch metrics | health check `/api/health` every 15 s, 3 misses |
| CloudFront `E2J4EQA5VW0HUG` + S3 `pilgrim-dot-farm-public-2` | `dyl45fh67kfm3.cloudfront.net` | media | free CloudWatch metrics (CloudFront in **us-east-1**) | |

### 1.2 AWS account state (477688903218, us-west-2)

- **Zero** CloudWatch alarms, dashboards and SNS topics.
- Log groups: `/aws/ecs/pg-app/app` (7 d), `/aws/ecs/pg-ecs` (7 d),
  `/aws/ecs/containerinsights/pg-ecs/performance` (1 d). The chat plan adds
  `/aws/pilgrim/chat-audit` and EMF metrics on stdout.
- IAM users: `mac-studio` (S3 rw, secret read, `logs:*`) and `kenny-logger`
  (`logs:*` only). This is the user the operator means by "credentials with
  permission to read and write logs". It has **no `cloudwatch:` permission**, so
  it cannot back a Grafana CloudWatch metrics data source as it stands (§5
  Step 7 decides where metrics read access lives: the bastion's instance role).
- Terraform: `~/projects/terraform-aws-pilgrim/modules/ec2` (`iam.tf`,
  `ecs.tf`, `db.tf`, `cache.tf`, `alb.tf`, …), local state file.

### 1.3 What the station already measures

`GET 127.0.0.1:5081/api/health` (admin listener, never wakes the station) is
the station's self-report (RADIO §11): `on_air`, `backends.{litellm, kokoro,
music, search, sfx}`, `music_backends`, `music_slots`, `search_last`,
`inventory.<type>.{have, target}` for 8 types, `rotation.<type>`,
`committed_coverage_s`, `committed_count`, `song_realtime`, `art.{backend,
with_art, without_art}`, `db.engine`, `cache.{healthy, ticks, failures,
last_publish_at, pending_players}`, `idle.{idle, since, wakes}`,
`listeners.{count, reporting, by_platform, hidden, not_running, sink_element,
resumed_total}`. Live at survey time: `on_air: true`, idle for 17 h,
`song: 0/2`, `sfx: false`, one music slot, cache `ticks 38779 / failures 1`.

`GET /api/station/state` (public, does not wake) adds the playhead:
`current_seq`, `current_type`, `current_offset_s`, `listeners`, `idle`.

The station log (`~/Library/Logs/pilgrim/station.out.log`, one text line per
event, `logging_setup.py`) carries the event stream Prometheus has no other
way to see. Last 200 k lines: `WARNING` events were `time_check` 8312,
`sfx.backend_retry` 2029, `sfx.failed` 676, `talk.motif_reuse` 268,
`song.brief_rejected` 30, `llm.call` 30, **`program.starved` 29**,
`request.failed` 7, `song.backend.down` 4, `request.backend_unavailable` 4,
`payment.refund_due` 1, `hotstate.down` 1, `search.degraded` 1. `song.produced`
lines carry `wall_s`, `rtf`, `duration_s`, `generator`, `backend`.

Housekeeping found on the way: `~/Library/Logs/pilgrim/gateway.access.log` is
37 MB with no rotation (Homebrew nginx, launchd); `station.out.log` is 30 MB
(launchd stdout, also unrotated; the `pilgrim/logs/station.log` copy rotates).

---

## 2. Principles and decisions

1. **Nothing here may wake, slow or touch playout** (AGENTS §1.1, §10). The
   only station endpoints monitoring calls are the admin `/api/health` and the
   public `/api/station/state`. Never `GET /api/station/program`, never `POST
   /api/station/heartbeat`. The station exporter is its own process and never
   imports `scheduler.py`, `producer.py`, `selector.py` or `payments.py`.
2. **Metered or blocking backends are never probed**: no Brave probe (AGENTS
   §2; the station reports `search_last`), no FLUX HTTP probe (a render blocks
   `/health` ~40 s, docs/backends.md §10; TCP connect to :8084 only), no song
   render, no LLM completion, **no Graph API call** (INSTAGRAM.md: the token is
   used only by `make reel-post`, and a probe would spend publishing quota).
   Reachability probes are `GET /health`, `GET /v1/models`, TCP or TLS
   handshakes.
3. **Cheap AWS first, paid where it adds a signal we cannot get otherwise.**
   Everything AWS already publishes for free (ALB, target group, ECS service
   CPU/memory, RDS basic, ElastiCache serverless, CloudFront, S3 storage,
   Billing) is pulled into Prometheus at a 5-minute cadence. Paid CloudWatch
   features are used when they answer a question nothing else can (operator,
   2026-11-07: not restricted to the free tier): Container Insights stays on,
   RDS Performance Insights (7-day retention) and Enhanced Monitoring at 60 s
   go on, CloudFront additional metrics go on, ALB access logs go to S3, and
   one Synthetics canary on pilgrim.farm is the AWS-side failsafe. No
   CloudWatch agent anywhere; nothing new is pushed into CloudWatch except the
   chat plan's EMF lines and the failsafe alarms. Costs in §7.
4. **Prometheus on the M5 is the LAN view, Prometheus on the bastion is the
   outside view and the VPC view.** Each one scrapes only what it can reach
   locally; they are each other's watchdog over the tailnet. Grafana lives on
   the M5 and reads both. Bastion data also answers the operator question "is
   it us or the home internet?".
5. **Config is the source of addresses** (AGENTS §1.4). Scrape target files
   for the station's backends are rendered from `pilgrim/config.yaml`
   (`hosts.*`, `songs.gen_backends`, `media.*`, the Valkey/RDS hosts from
   `.env`) by a small tool, never typed into `prometheus.yml`. Hosts the station
   does not know (the GPU boxes' node exporters, the bastion) live in one
   checked-in targets file with hostnames, not secrets.
6. **No secrets in the repo** (AGENTS §1.7). Grafana's admin password, the
   notification tokens and any AWS key live in `~/pilgrim-monitoring/` (mode
   600) or in Grafana's own store. The bastion uses its instance role for
   CloudWatch; the M5 needs no AWS key for metrics.
7. **Nothing public.** Prometheus, Alertmanager and Grafana bind to loopback
   plus the tailnet address. Grafana is reached over the tailnet (§6 Q1
   decides plain `100.114.40.70:3000` vs `tailscale serve`).
8. **Dashboards and rules are code**: provisioned JSON and YAML under
   `pilgrim/deploy/monitoring/`, installed by scripts in the style of
   `pilgrim/deploy/mac/origin.sh` (render → `~/pilgrim-monitoring/` → launchd /
   systemd units). No click-ops that the repo does not reproduce.
9. **Alerts come after dashboards.** Steps 1–8 build the signals and the views;
   Step 9 ships the rules silent (visible in Grafana, routed to no one), Step
   10 wires receivers and escalation. The catalogue (§4) is written now so the
   signals are the right ones.
10. **Metric prefix is `pg_`, not `pilgrim_`.** Everything on this farm is
   Pilgrim; a `pilgrim_` prefix would carry no information. `pg_` is the
   fleet's existing short form (`pg-app`, `pg-alb`, `pg-db`, `pg-cache`) and
   keeps the station's own series distinct from yace's `aws_*` and the
   exporters' `node_*`/`probe_*` series.

---

## 3. Signals

### 3.1 Station (M5, `pilgrim/monitoring/exporter.py`, port 9101)

One asyncio process, its own launchd unit `farm.pilgrim.exporter`, Prometheus
text format hand-rolled (the format is trivial; no new dependency, AGENTS §3).
Every 15 s it reads the admin `/api/health` and the public
`/api/station/state`, tails the station log, and asks `tailscale status
--json` for the VPC route. All metrics carry no labels that could hold a
listener or player id.

| Metric | Type | From |
|--------|------|------|
| `pg_on_air`, `pg_idle`, `pg_idle_since_seconds`, `pg_idle_wakes_total` | gauge/counter | health |
| `pg_listeners`, `pg_listeners_reporting`, `pg_listeners_by_platform{platform}`, `pg_listeners_hidden`, `pg_listeners_not_running`, `pg_listeners_sink_element`, `pg_listeners_resumed_total` | gauge | health |
| `pg_committed_coverage_seconds`, `pg_committed_items` | gauge | health |
| `pg_inventory_have{type}`, `pg_inventory_target{type}`, `pg_rotation{type}` | gauge | health |
| `pg_backend_up{backend=litellm\|kokoro\|music\|search\|sfx}`, `pg_music_backend_up{name}`, `pg_music_slots{name}` | gauge | health |
| `pg_search_last_ok`, `pg_search_last_at_seconds` | gauge | health (Brave, no probe) |
| `pg_song_realtime_ratio` | gauge | health `song_realtime` |
| `pg_art_backend{state}`, `pg_art_items{with_art=yes\|no}` | gauge | health |
| `pg_db_engine{engine}`, `pg_cache_enabled`, `pg_cache_healthy`, `pg_cache_ticks_total`, `pg_cache_failures_total`, `pg_cache_last_publish_age_seconds`, `pg_cache_pending_players` | gauge/counter | health |
| `pg_current_type{type}`, `pg_current_offset_seconds`, `pg_program_revision`, `pg_rating_open` | gauge | state |
| `pg_log_events_total{logger,event,level}` | counter | station log tail; `event` is the dotted name (`program.starved`, `sfx.failed`, `request.failed`, `payment.refund_due`, `hotstate.down`, `song.backend.down`, …). Bounded: only `WARNING`/`ERROR` lines plus an allowlist of `INFO` events (`song.produced`, `produced`, `media.published`, `request.*`, `idle.*`, `listener.telemetry`) |
| `pg_song_render_seconds` / `pg_song_render_rtf` | histogram | `song.produced` lines (`wall_s`, `rtf`), labels `generator` |
| `pg_tailscale_route_present{route="172.31.0.0/16"}` | gauge | `tailscale status --json` on the M5 (the peer's AllowedIPs) |
| `pg_exporter_scrape_ok{source}`, `pg_exporter_last_success_seconds{source}` | gauge | the exporter about itself |
| `pg_launchd_unit_running{label}` | gauge | `launchctl print gui/<uid>/<label>` for `farm.pilgrim.station`, `farm.pilgrim.gateway`, `farm.pilgrim.chat-worker` |
| `pg_process_listening{port}` | gauge | `lsof -iTCP:<port> -sTCP:LISTEN` for mlx-serve :11234 and FLUX :8084 (the two bare processes) |

Request-line depth (`/api/requests`, RADIO §11) and payment stages are **not**
scraped in this release: the payment path is the chat agent's and the
operator's territory, and the log counters (`request.*`, `payment.refund_due`)
already give the alertable events. Revisit in Step 11.

### 3.2 Hosts (node_exporter :9100 everywhere, GPU on the CUDA boxes)

- M5: `brew install node_exporter` (Homebrew service, launchd). Collectors:
  cpu, mem, disk, filesystem, network, loadavg, thermal where available,
  textfile.
- twr, spx1, `.89`: already running; keep them, add them to targets. spx0: open
  `:9100` to the M5 (Step 4) or restart the container with host networking.
- bastion: node_exporter binary under systemd, bound to the tailnet address.
- GPU: `nvidia_gpu_exporter` (utkuozdemir; single binary, wraps `nvidia-smi`,
  arm64 builds exist) on twr and `.89` → utilisation, memory, temperature,
  power. On the Sparks the GB10 reports memory `[N/A]`, so only utilisation,
  temperature and power are meaningful; still worth it to see whether the
  music boxes are busy with something else (today's DeepSeek job).
- Disk pressure is the first real alert this survey found (§1.1): 94/96/99 %.

### 3.3 Model servers and the station's own front door (blackbox_exporter on the M5, :9115)

Targets rendered from `config.yaml` (§2.5). Modules: `http_2xx`, `http_2xx_json`
(regex on the body), `tcp_connect`, `tls_connect`.

| Target | Module | Expect |
|--------|--------|--------|
| `hosts.kokoro` `/health` | http_2xx | 200 |
| `hosts.sfx` `/health` | http_2xx | 200 (down today) |
| each `songs.gen_backends` entry: direct boxes `GET {url}/v1/models`; the LiteLLM pool entry: `GET hosts.litellm/models` with the bearer, body contains `models.music` | http_2xx_json | same rule as `song_backend_health()` |
| the three deployments behind the pool, `192.168.68.88:8080`, `192.168.68.56:8080`, `127.0.0.1:11234` (`/v1/models`) | http_2xx | tells which box is out even when the pool says "up" |
| `hosts.litellm` `/health/liveliness` | http_2xx | 200 (observed) |
| `hosts.mcp` TCP | tcp_connect | |
| `hosts.flux` TCP :8084 **only** | tcp_connect | never HTTP (§2.2) |
| station `127.0.0.1:5080/api/health`, `127.0.0.1:5081/api/health`, gateway `127.0.0.1:8090/api/health`, `https://justins-mac-studio.tail063158.ts.net/api/health` (tailscale serve) | http_2xx | 200 and `"on_air": ?true` |
| Valkey host from `REDIS_URL` | tls_connect | TLS 1.3 handshake over the bastion route |
| RDS host from `DATABASE_URL` | tcp_connect | |
| `https://pilgrim.farm/api/health` (the inside view) | http_2xx_json | 200, `on_air` |

LiteLLM's own `/metrics` is a Step 1 question: if it is reachable with the
token, scrape it directly (per-model latency, tokens, failures per
deployment); if it is enterprise-gated (the 307 suggests so) the station's
`llm.call` log counters and the blackbox probe are enough.

### 3.4 Public path and VPC (bastion Prometheus)

- blackbox from the bastion: `https://pilgrim.farm/api/health` (Cloudflare →
  ALB → task, expects `on_air`), `https://pilgrim.farm/api/station/state`,
  `https://pg-alb-….us-west-2.elb.amazonaws.com/api/health` with `Host:
  pilgrim.farm` (bypasses Cloudflare), `HEAD` of one known media object on
  `dyl45fh67kfm3.cloudfront.net` (CORS header present, 200), TLS expiry on
  `pilgrim.farm`; relay `http://127.0.0.1:4000/relay-health` (200) and
  `/v1/models` (expects **401**: the whole chain to LiteLLM works); Valkey TLS
  and RDS TCP from inside the VPC; the M5's `https://justins-mac-studio.tail…
  /api/health` (the Step 8 "Tailscale serve still works" probe, which is also
  what the relay depends on).
- node_exporter + textfile collector: `tailscale status --json` →
  `tailscale_primary_route{route="172.31.0.0/16"} 1`, relay container running
  (`docker inspect`), `tailscale_peer_online{peer="justins-mac-studio"}`.
- **Watchdog**: scrape the M5's Prometheus `/-/healthy` over the tailnet
  (`up{job="m5-prometheus"}`), and the M5 scrapes the bastion's in return.
- yace (yet-another-cloudwatch-exporter) with the instance role, 300 s
  period, only the namespaces below (§3.5). Grafana on the M5 then never
  calls CloudWatch for metrics.

### 3.5 CloudWatch, free tier only (via yace, 5-minute cadence)

| Namespace | Metrics | Why |
|-----------|---------|-----|
| `AWS/ApplicationELB` (`pg-alb`, `pg-http`) | `RequestCount`, `HTTPCode_Target_2XX/4XX/5XX_Count`, `HTTPCode_ELB_5XX_Count`, `TargetResponseTime` (p50/p99), `HealthyHostCount`, `UnHealthyHostCount`, `ActiveConnectionCount` | listener-facing errors and latency; `HealthyHostCount` is the task-count signal without Container Insights |
| `AWS/ECS` (`pg-ecs`/`pg-app`) | `CPUUtilization`, `MemoryUtilization` | the only free ECS metrics |
| `AWS/RDS` (`pg-db`) | `CPUUtilization`, `FreeableMemory`, `FreeStorageSpace`, `DatabaseConnections`, `ReadIOPS`, `WriteIOPS`, `ReadLatency`, `WriteLatency`, `BurstBalance`/`EBSByteBalance%` | t4g.micro has little headroom; storage is 20 GB |
| `AWS/ElastiCache` (serverless `pg-cache`) | `ElastiCacheProcessingUnits`, `BytesUsedForCache`, `CurrConnections`, `ThrottledCmds`, `CacheHits/Misses`, `NetworkBytesIn/Out`, `SuccessfulReadRequestLatency/WriteRequestLatency` | the 1000 ECPU/s cap is the chat plan's stated ceiling |
| `AWS/CloudFront` (**us-east-1**, `E2J4EQA5VW0HUG`) | `Requests`, `BytesDownloaded`, `4xxErrorRate`, `5xxErrorRate`, `TotalErrorRate` | media delivery |
| `AWS/S3` (daily) | `BucketSizeBytes`, `NumberOfObjects` | library growth |
| `AWS/Billing` (**us-east-1**, needs "receive billing alerts" enabled once) | `EstimatedCharges` total and per service | the monthly number on the dashboard |
| `AWS/EC2` (`i-052b107fc0251d8c2`) | `CPUUtilization`, `StatusCheckFailed`, `NetworkIn/Out`, `CPUCreditBalance` if applicable | bastion, 5-minute basic metrics are free |
| chat EMF namespace (SHEEP_PEN_PLAN) | `chat_messages_per_s`, `chat_moderation_backlog`, worker heartbeat metrics | read-only here; owned by the chat plan |

### 3.6 Reels (Instagram, operator-triggered)

`make reel-post` (INSTAGRAM.md; `docs/instagram.md`) is not a station task and
has no schedule, so there is nothing to poll: monitoring only **records** what
the tool did. It never calls the Graph API (see §2.2, §8 rule 1) — the token,
the quota and the container status can only be learned by posting, and a
synthetic post would spend one of the 100/24 h.

Source: `pilgrim/logs/reels/*.json` (one ledger per attempt, rewritten at each
stage; gitignored). The tool logs to stderr, not `station.log`, so §3.1's log
tail cannot see it — a small writer, `pilgrim/monitoring/reels.py` (Step 3),
emits a textfile that the M5 node_exporter's textfile collector serves:
`~/pilgrim-monitoring/textfile/reels.prom`. It reads only that directory — no
store, no database, no network (consistent with §8 rule 2).

| Metric | Type | From |
|--------|------|------|
| `pg_reel_posts_total` | counter | ledgers that reached `status=published` (cumulative; the writer keeps its own `reels.counters.json` so the counter survives restarts and a deleted ledger) |
| `pg_reel_failures_total{stage}` | counter | ledgers with `status=failed`; `stage` is `failed_at` (`building`\|`built`\|`captioned`\|`uploaded`\|`container`) |
| `pg_reel_last_posted_timestamp_seconds` | gauge | newest `posted_at` across ledgers (0 when none) |
| `pg_reel_quota_usage`, `pg_reel_quota_total` | gauge | the `quota` block of the most recent publish attempt; **as of the last post**, not live |
| `pg_reel_ledgers{status}` | gauge | ledgers currently on disk, by `status` (`prepared` = `uploaded` with no `media_id`, i.e. never published) |
| `pg_reel_writer_last_success_seconds` | gauge | the writer about itself (feeds #30) |

Labels carry nothing private: no caption text, no permalink, no media id, no
token (§8 rule 3). Deliberately not collected: the caption (it is copy, not a
signal), the MP4 size (`media:` S3 metrics already cover the bucket), and the
`reels/` object's presence (bastion probe #23 covers CloudFront).

Logs stay in CloudWatch (`/aws/ecs/pg-app/app`, the chat audit group). Grafana
gets a CloudWatch **Logs** data source using `kenny-logger` (its `logs:*` covers
Logs Insights) so container and chat JSON lines are searchable next to the
metrics; this is the one place the "logs" IAM user is used. The M5's own logs
are files; Loki is Phase 2 (§6 Q5).

---

## 4. What we will alert on, and who answers

Three responders, routed by two labels on every rule: `severity`
(`page|agent|digest`) and `runbook` (an anchor in `docs/monitoring.md`).

- **P1 `page`: a human, now.** Listeners are, or are about to be, hearing
  silence, or money is involved, or the whole site is dark. Push notification
  plus email; repeats every 30 min until acknowledged.
- **P2 `agent`: an agent first, a human if it is still firing.** Something is
  degraded but the committed window is still playing. Alertmanager posts the
  alert JSON to the agent receiver (Step 10): a non-interactive Claude Code
  run with the runbook anchor, read access to this repo, the logs and both
  Prometheus, and a **short fixed allowlist** of remediations (kickstart a
  `farm.pilgrim.*` launchd unit, `relay.sh start`, re-approve the Tailscale
  route, clear a backend cooldown by restarting the exporter's view: nothing
  that touches `station.db`, the library, Stripe or the GPU boxes' jobs). It
  posts a summary to the operator channel. If the alert is still firing after
  `repeat_interval` (30 min default, 2 h for inventory alerts) Alertmanager
  re-routes it to the human receiver with the agent's summary attached.
- **P3 `digest`: a daily message.** Grouped at 09:00 local; nothing pages.

Every alert also lands in Grafana's alert list, which is where silences live.
A **dead man's switch** closes the loop: both Alertmanagers send a `Watchdog`
alert every minute to a healthchecks.io check (free tier); when it goes quiet
the operator is told by a path that owns none of our infrastructure.

### 4.1 Catalogue

| # | Alert | Condition (for) | Source | Sev | Runbook gist |
|---|-------|------------------|--------|-----|--------------|
| 1 | **Site dark** | `probe_success{job="public",target=~"pilgrim.farm.*"} == 0` (3 m) from the bastion | bastion blackbox | page | Cloudflare → ALB → task → Valkey; check #2, #3, #5 first |
| 2 | **Public off-air** | body `on_air` false for 3 m | bastion blackbox | page | station `alive` key expired: station down, route down, or publisher failing (#12) |
| 3 | **No healthy web task** | `aws_applicationelb_healthy_host_count < 1` (2 m) | yace **and** a CloudWatch alarm → SNS (failsafe) | page | ECS events, task logs, circuit breaker |
| 4 | **Dead air imminent** | `pg_listeners > 0 and pg_committed_coverage_seconds < 60` (1 m) or `increase(pg_log_events_total{event="program.starved"}[10m]) > 0 while listeners > 0` | M5 exporter | page | inventory, backends, `producer` log |
| 5 | **M5 unreachable** | `up{job="m5-prometheus"} == 0` on the bastion (5 m) | bastion | page | home internet / Mac asleep / Tailscale |
| 6 | **Bastion unreachable** | `up{job="bastion-prometheus"} == 0` on the M5 (5 m) | M5 | page (the relay and the VPC route are down with it) | EC2 status checks, SSM |
| 7 | **Money** | `increase(pg_log_events_total{event="payment.refund_due"}[1h]) > 0`; `request.failed` with `paid` in the line | M5 exporter | page | PAYMENT.md §14 refund procedure; human only, never the agent |
| 8 | **Station process down** | `pg_launchd_unit_running{label="farm.pilgrim.station"} == 0` (2 m) or `probe_success{target="127.0.0.1:5081"} == 0` | M5 | agent | `launchctl kickstart -k gui/$(id -u)/farm.pilgrim.station`; read `station.err.log` first |
| 9 | **Gateway / tailscale serve down** | gateway `:8090` or the `ts.net/api/health` probe fails (3 m) | M5 + bastion | agent | `origin.sh status`; `tailscale serve status` |
| 10 | **Backend down** | `pg_backend_up{backend=~"kokoro\|litellm\|music"} == 0` (10 m); `sfx == 0` (30 m) | M5 exporter + blackbox | agent | which box; is a foreign job holding the GPU (nvidia exporter); SSH `docker ps`; **report, don't restart other people's jobs** |
| 11 | **Music pool degraded** | `sum(pg_music_slots) <= 1` (1 h) or a direct deployment probe down (30 m) | M5 | agent | today's state: audiocpp boxes replaced by a DeepSeek job |
| 12 | **Hot-state publisher failing** | `pg_cache_healthy == 0` (2 m) or `rate(pg_cache_failures_total[10m]) > 0` or `pg_cache_last_publish_age_seconds > 3 × cache.alive_ttl_s` | M5 exporter | agent (→ page via #2 if the public side notices) | route, Valkey TLS probe, ECPU throttling (#20) |
| 13 | **VPC route missing** | `pg_tailscale_route_present == 0` (5 m) on either side | M5 + bastion textfile | agent | re-advertise / re-approve the route (`deploy/README.md` "Bastion subnet router") |
| 14 | **Relay broken** | `relay-health != 200` or `/v1/models` not 401 (5 m) | bastion blackbox | agent | `relay.sh start`; requests moderation on the web tier fails closed while it is down |
| 15 | **Inventory below target** | `pg_inventory_have < pg_inventory_target` for `song` (1 h), `dj_talk`/`news` (30 m), others (3 h) | M5 exporter | agent, repeat 2 h | which pipeline; `produced`/`*.rejected` counters; backends |
| 16 | **News stale** | `news` inventory 0 for 2 × `news.refresh_s`, or `pg_search_last_ok == 0` for 2 h | M5 | agent / digest | Brave quota (BRAVE.md), `news.search_degraded` |
| 17 | **Renders slower than real time** | `pg_song_realtime_ratio > 1.0` (1 h) or render histogram p50 > 2 × duration | M5 | agent | box contention; `rtf` per generator |
| 18 | **Disk** | `node_filesystem_avail_bytes / size < 10 %` (15 m) on any host; `< 5 %` → page | node_exporter | agent (page at 5 %) | **report only on the GPU boxes** (their disks hold other people's models); M5: `pilgrim/library/` growth, logs |
| 19 | **Host down** | `up{job="node"} == 0` (10 m) | both Prometheus | agent | SSH; power; the Sparks reboot together |
| 20 | **Valkey near the cap** | `ElastiCacheProcessingUnits` > 80 % of 1000/s (10 m) or `ThrottledCmds > 0` | yace | agent | chat load vs heartbeats (SHEEP_PEN_PLAN §0.6.7); the cap is terraform |
| 21 | **RDS pressure** | `FreeStorageSpace < 2 GB`, `CPUUtilization > 80 %` (15 m), `DatabaseConnections > 60`, `BurstBalance < 20 %` | yace + CloudWatch alarm on storage (failsafe) | agent | slow queries; the 7-day chat purge; storage autoscaling is terraform |
| 22 | **Web tier errors** | ALB target 5xx > 1 % of requests (5 m), `TargetResponseTime p99 > 2 s` (10 m), ECS CPU or memory > 80 % (10 m) | yace | agent | task logs in CloudWatch; `ecs_count` |
| 23 | **CloudFront errors** | `5xxErrorRate > 1 %` (10 m) or the media object probe fails | yace + bastion blackbox | agent | S3 object present? CORS function (AGENTS §10) |
| 24 | **Listeners misbehaving** | `pg_listeners_not_running / reporting > 0.5` with `reporting >= 5` (10 m); underrun events rising | M5 | digest | RADIO §9.2; reported, never acted on |
| 25 | **Art backlog** | `pg_art_items{with_art="no"} > 0` (2 h) or `art.backend != ok` (2 h) | M5 | digest | FLUX bare process; TCP probe |
| 26 | **mlx-serve / FLUX process gone** | `pg_process_listening{port=~"11234\|8084"} == 0` (10 m) | M5 | agent (restart is **operator-defined**, §6 Q6) | they are not under launchd today |
| 27 | **TLS expiry** | `probe_ssl_earliest_cert_expiry - time() < 14 d` for `pilgrim.farm` and the `ts.net` cert | blackbox | digest | Cloudflare renews; Tailscale renews on serve |
| 28 | **Chat worker heartbeat** | the chat plan's heartbeat/EMF signal absent (5 m) | yace / chat plan | agent | SHEEP_PEN_PLAN; `chat_worker.sh status`. Wired in Step 11 |
| 29 | **Spend** | AWS Budget 80 % forecast; `EstimatedCharges` month-to-date above the operator's number | AWS Budgets (free ×2) + yace | digest (page above a hard ceiling) | Cost Explorer by service |
| 30 | **Monitoring itself** | `pg_exporter_scrape_ok == 0` (5 m); yace scrape errors; Alertmanager cluster; the reel textfile writer stale (`pg_reel_writer_last_success_seconds`, §3.6); `Watchdog` silence → healthchecks.io | both | agent / external | |
| 31 | **Log noise** | `rate(pg_log_events_total{level="WARNING"}[1h])` > 5 × its 7-day median | M5 | digest | today: `sfx.backend_retry` 2029 lines while SFX is down |
| 32 | **Reels stale** | `pg_reel_posts_total > 0 and time() - pg_reel_last_posted_timestamp_seconds > 30 d` (1 d) | M5 textfile (§3.6) | digest | Usually an expired `INSTAGRAM_TOKEN`: the next `--publish` answers `OAuthException 190`; the operator regenerates the system-user token in Business Settings (never paste it) |
| 33 | **Reels quota nearly used** | `pg_reel_quota_usage >= 0.9 * pg_reel_quota_total` (1 h) | M5 textfile (§3.6) | digest | 100 posts / 24 h; `make reel-post` refuses at 100/100 before creating a container |

---

## 5. Steps

Each step ends with the AGENTS §7 checks that apply (`make lint`, `make test`;
`make sim`/`make e2e` are untouched by this plan but still run), a commit citing
RADIO.md sections (§2, §11, §14 are the ones referenced), and the §8 report.
Everything lands in new files; the one exception is noted in Step 11.

**Step 1 — Verify the survey, write `docs/monitoring.md`.** Probe, read-only:
LiteLLM `/metrics` with the token (is it metrics or a login redirect?),
mlx-serve's endpoints (anything besides `/v1/models`?), spx0's `:9100` (ufw?
docker network?), the identity and SSH route to `192.168.68.89`, whether
`nvidia-smi` on the Sparks gives utilisation (it did give the name), the
bastion's `yum`/binary path for node_exporter, blackbox and yace, Homebrew
formula versions for prometheus/grafana/alertmanager/node_exporter/
blackbox_exporter on the M5. Record shapes and versions in `docs/monitoring.md`
§1 (the way `docs/backends.md` records backends; do not edit backends.md).
*Done when* the doc exists with observed outputs and §1.1's open cells are
filled or marked unknown with a reason.

**Step 2 — M5 stack.** `pilgrim/deploy/monitoring/mac/monitoring.sh
install|status|uninstall`: Homebrew `prometheus`, `alertmanager`, `grafana`,
`node_exporter`, `blackbox_exporter`; configs rendered from
`pilgrim/deploy/monitoring/mac/*.template` into `~/pilgrim-monitoring/`
(mode 700); launchd units `farm.pilgrim.prometheus`, `.alertmanager`,
`.grafana`, `.node-exporter`, `.blackbox` written the same way `origin.sh`
writes its plists (no secrets in plists). Prometheus binds `127.0.0.1` and
the tailnet address, 30-day retention, scrape 15 s. Grafana binds the same,
provisioned data sources (M5 Prometheus, bastion Prometheus placeholder,
Alertmanager), admin password set once by the operator into
`~/pilgrim-monitoring/grafana.env`. `make monitoring-check` renders every
template and runs `promtool check config/rules` and `amtool check-config`
(the `deploy-check` pattern). *Done when* `monitoring.sh status` shows the
five units running, Grafana answers on the tailnet address, Prometheus
scrapes itself and node_exporter, `make monitoring-check` passes.

**Step 3 — Station exporter.** `pilgrim/monitoring/exporter.py` (§3.1) with
`pilgrim/monitoring/textfmt.py` (the exposition format) and
`pilgrim/monitoring/logtail.py` (the event counter: follows the file across
rotation, parses `<ts> <LEVEL> <logger> <event> k=v…`, allowlist of INFO
events). Config: a small `monitoring:` block is **not** added to
`config.yaml` in this step (the chat agent owns it); the exporter reads the
station's admin `host:port` from `load_config()` and takes its own bind/port
and log path from CLI flags with defaults, documented in `docs/monitoring.md`.
Tests in `pilgrim/tests/test_monitoring_exporter.py` against canned health
JSON and log fixtures (no network, no real station; AGENTS §1.2), including:
no label ever contains a player id, the exporter never requests `/program` or
`/heartbeat`, rotation is followed. launchd unit `farm.pilgrim.exporter` added
to `monitoring.sh`. *Done when* `curl 127.0.0.1:9101/metrics` shows the §3.1
families against the live station and `make test` passes.

**Step 4 — LAN hosts.** `pilgrim/deploy/monitoring/hosts/install.sh <host>`
(idempotent over SSH; `twr`, `spx0`, `spx1`, the Kokoro box once named):
leave the existing node_exporter containers alone where they are reachable;
fix spx0 (firewall rule for the M5's address or host networking, whichever
Step 1 found); add `nvidia_gpu_exporter` as a systemd unit on each CUDA box;
`pilgrim/deploy/monitoring/targets/hosts.yml` (file_sd) lists them by role
(`llm`, `music`, `tts`, `sfx`). The install script is read-mostly: it adds a
unit and a firewall rule and nothing else; it never touches the model
containers or disks. *Done when* `up{job="node"} == 1` for every LAN host and
`nvidia_gpu_utilization` is present for twr and `.89` (and whatever the
Sparks give).

**Step 5 — Blackbox targets from config.** `pilgrim/tools/render_monitoring.py`
reads `pilgrim/config.yaml` (+ `.env` for the Valkey and RDS hostnames only;
it prints hostnames, never credentials) and writes
`~/pilgrim-monitoring/targets/backends.yml` for the §3.3 table. The template
forbids FLUX HTTP, Brave and `/api/station/program` by construction: the
render tool has an explicit deny list (which also refuses
`graph.facebook.com`, §2.2) and a unit test asserting none of those appear in
its output. *Done when* every §3.3 row shows in Prometheus with the
expected `probe_success`, and the tests pass.

**Step 6 — Bastion stack.** `pilgrim/deploy/monitoring/bastion/monitoring.sh
install|status|uninstall` over `ssh pf2`: node_exporter, blackbox_exporter,
yace and Prometheus as systemd units from pinned release tarballs (AL2023,
x86_64), everything bound to `127.0.0.1` and the tailnet address (no security
group change). Scrape jobs per §3.4; 15-day retention. The textfile collector
script writes the Tailscale route and relay-container gauges every minute
(cron). Tailnet policy: add `pilgrim-origin → tag:radio-relay tcp:9090` and
`tag:radio-relay → pilgrim-origin tcp:9090` to
`pilgrim/deploy/tailscale/policy.example.hujson` (new grants only). The M5
Grafana's second data source points at `100.66.128.118:9090`. *Done when*
both watchdog `up` series are 1 from both sides and `probe_success` for
`pilgrim.farm` is visible in the M5 Grafana via the bastion source.

**Step 7 — CloudWatch, cheaply.** In `~/projects/terraform-aws-pilgrim/
modules/ec2`: (a) a read-only CloudWatch policy on the **instance role**
`pg-s3-read` (`cloudwatch:GetMetricData`, `ListMetrics`,
`GetMetricStatistics`, `DescribeAlarms`, `tag:GetResources`,
`ec2:DescribeInstances/Tags`, `apigateway:GET` not needed) so yace runs with no
key; (b) `cloudwatch:` **read** added to `kenny-logger` only if the operator
wants Grafana's CloudWatch data source to show metrics too (§6 Q2); the Logs
data source needs only what it has; (c) the failsafe alarms → one SNS topic
with an email subscription: TG `HealthyHostCount < 1` (2 of 2 × 1 min), ALB
`HTTPCode_Target_5XX_Count` > 5 %, RDS `FreeStorageSpace < 2 GB`, Valkey
`ThrottledCmds > 0`, EC2 `StatusCheckFailed` on the bastion; (d) an AWS
Budget at the operator's monthly number with 80 % forecast and 100 % actual
notifications; (e) enable billing metrics (account setting, once);
(f) the paid signals from §2.3 in terraform: Container Insights kept on, `performance_insights_enabled = true` (7-day retention) and `monitoring_interval = 60` with a monitoring role on `pg-db`, CloudFront additional metrics (`aws_cloudfront_monitoring_subscription`), ALB access logs to a private bucket with 30-day expiry, and one `aws_synthetics_canary` hitting `https://pilgrim.farm/api/health` every 5 min with its own alarm. yace then also collects `RunningTaskCount`, RDS `DBLoad`, CloudFront `CacheHitRate` and `OriginLatency`, and `SuccessPercent` from the canary. yace config
(`pilgrim/deploy/monitoring/bastion/yace.yml`) lists only §3.5. *Done when*
`aws_applicationelb_healthy_host_count` and the RDS/Valkey/CloudFront series
appear in the bastion Prometheus, the SNS email is confirmed, and
`terraform plan` shows only the IAM, alarm, SNS and budget additions.

**Step 8 — Dashboards as code.** `pilgrim/deploy/monitoring/grafana/
dashboards/*.json`, provisioned: **Station** (on-air/idle, listeners,
coverage, inventory vs targets, backends and slots, cache publisher, renders
per generator, log events, Reels: `pg_reel_posts_total`, failures by stage,
last-post age and quota vs total, §3.6), **Hosts** (node + GPU per box, disks front and
centre), **Model servers** (probe status/latency matrix, LiteLLM metrics if
Step 1 found them), **Public path** (bastion probes, ALB, CloudFront, TLS
expiry), **Data tier** (RDS, Valkey ECPU vs the cap), **Spend** (Billing by
service, S3 size). One row per dashboard answers "is it us or the internet"
by placing the M5 and bastion views of the same probe side by side. *Done when*
every §3 metric family is on a panel and the operator has looked at them.

**Step 9 — Rules, silent.** `pilgrim/deploy/monitoring/rules/*.yml` for the §4
catalogue with `severity` and `runbook` labels, recording rules for the
ratios, loaded by both Prometheus (bastion rules for #1–#3, #5, #13–#14,
#20–#23, #29; M5 for the rest). Alertmanager routes everything to a
`null` receiver and to Grafana's alert list; the `Watchdog` rule exists but
points nowhere yet. `docs/monitoring.md` §3 gets one runbook section per
alert. *Done when* `promtool test rules` passes against unit fixtures for at
least #1, #2, #4, #12, #15, #18 and the live alert list shows today's real
state (#10 sfx, #11 one slot, #15 song 0/2, #18 three disks) firing.

**Step 10 — Receivers and escalation (alerting proper, later).** Human
receiver (operator choice, §6 Q4: ntfy, Pushover or email+SMS) for `page`;
the daily `digest` receiver; the agent receiver: a small webhook on the M5
(`pilgrim/monitoring/alert_receiver.py`, loopback only) that writes the alert
to a queue file and launches one non-interactive Claude Code run per alert
group with the runbook anchor, the fixed remediation allowlist, and a 20-minute
budget, posting its summary to the human channel; `repeat_interval` re-routes
unresolved `agent` alerts to `page`. healthchecks.io check for the Watchdog.
*Done when* a synthetic alert walks the whole path (agent summary posted,
then escalation to the human receiver after the interval) and a silenced
Watchdog produces the external notification.

**Step 11 — After the chat work merges (API and config changes).** (a) A
`monitoring:` block in `config.yaml` (exporter bind/port, log path, scrape
interval) replacing the Step 3 CLI defaults — RADIO §12 updated in the same
change. (b) `GET /metrics` on the **admin** listener in `server.py` (RADIO
§11), folding the exporter's health mapping into the station so it stops
polling itself; the log tail and launchd/process checks stay in the
exporter. (c) `GET /metrics` on the Fargate web tier, scraped by the bastion
over the private IP (one `pg-ec2`-sourced security-group rule) and the
chat-worker heartbeat (#28). (d) The chat EMF namespace added to yace. These
are the only steps that touch RADIO.md or the chat agent's files.

---

## 6. Open questions (operator)

1. **Grafana access**: plain `http://100.114.40.70:3000` over the tailnet
   (simplest, ACL-gated), or `tailscale serve --https=8443` for a certificate?
   Recommendation: tailnet IP now, serve later if a phone is used.
2. **Where metrics read access lives**: the bastion's instance role (no keys,
   recommended) or an access key for `kenny-logger` on the M5 (as described in
   the request). The plan does the role and uses `kenny-logger` only for the
   CloudWatch Logs data source.
3. ~~Container Insights on or off~~ Resolved 2026-11-07: the operator is not
   restricted to the free tier. Container Insights stays on and the other paid
   signals in §2.3 are added in Step 7.
4. **Human channel** for `page`: ntfy (self-hostable, free), Pushover (one-off
   fee), or email + carrier SMS. And the hard spend ceiling for #29.
5. **Logs on the M5**: stay as files, or add Loki + Promtail (Homebrew) in a
   Phase 2 so station lines are searchable in Grafana alongside CloudWatch
   Logs? Recommendation: Phase 2, after Step 9 shows which log counters are
   actually used.
6. **mlx-serve and FLUX** run as bare processes on the M5. Should they get
   launchd units (KeepAlive) so #26 becomes a restart instead of a page? Their
   start commands are known (`mlx-serve --serve --model …/MiniMax-Music3-MLX-
   Serve-8bit --host 0.0.0.0 --port 11234`; `serve.py --model 6bit --port
   8084` under Python 3.15). Out of scope for this plan unless you say so.
7. **The GPU boxes are shared** with other work (today a DeepSeek job holds
   both Sparks and the music/SFX servers are not running). The plan only
   observes and reports on them; the agent responder never restarts anything
   there. Confirm.
8. **Disk cleanup on twr/spx0/spx1** (94/96/99 %) is a human job and the first
   thing the dashboards will show. Not part of this plan.
9. **Retention**: 30 d on the M5 and 15 d on the bastion TSDB; CloudWatch log
   groups stay at 7 d. OK?

---

## 7. Rough monthly cost (verify in Cost Explorer after Step 7)

| Item | Estimate | Note |
|------|----------|------|
| Prometheus, Grafana, Alertmanager, exporters, yace | $0 | OSS on hardware we already run; the bastion has 3.2 GB RAM free |
| CloudWatch metrics for ALB/ECS/RDS/ElastiCache/CloudFront/S3/EC2 basic/Billing | $0 | vended metrics |
| yace `GetMetricData`: ~60 metrics every 300 s | ~$5 | $0.01 per 1000 metrics requested; excluded from the free tier. Halve it with 600 s where 5-minute resolution is enough (S3, Billing, CloudFront) |
| 5 standard CloudWatch alarms | $0.50 | $0.10 each |
| SNS email | $0 | first 1000 |
| AWS Budgets | $0 | first two free |
| Container Insights (kept) | ~$3–8 | per-task custom metrics, one task |
| RDS Performance Insights, 7-day retention | $0 | free retention tier; Enhanced Monitoring at 60 s ≈ $1–2 in log ingestion |
| CloudFront additional metrics | ~$1 | per distribution |
| ALB access logs to S3 | cents | queried with Athena only on demand |
| Synthetics canary, 5-min cadence | ~$7–10 | the AWS-side failsafe, independent of both Prometheus |
| Existing log ingestion (`/aws/ecs/pg-app/app`, chat audit) | unchanged | $0.50/GB ingested, owned by the chat plan |
| healthchecks.io | $0 | free tier |

---

## 8. Hard rules for this plan

In addition to AGENTS §1:

1. Monitoring never calls `GET /api/station/program` or `POST
   /api/station/heartbeat`, never probes Brave, FLUX or the Meta Graph API
   over HTTP, never asks a model for anything. Enforced by the render tool's
   deny list and a test.
2. The exporter, the receiver and the render tool never import
   `pilgrim/scheduler.py`, `pilgrim/producer.py`, `pilgrim/selector.py` or
   `pilgrim/payments.py`, and never open `station.db` or `DATABASE_URL`.
3. No metric label or log line produced here carries a player id, voter id,
   IP, email, token, URL with credentials, or chat text.
4. Nothing binds a public address. Cross-site traffic is tailnet only.
5. The agent responder's remediation allowlist is in the repo
   (`docs/monitoring.md` §4) and anything not on it is a report, not an
   action. Money (#7) is never routed to the agent.
6. Until the chat work merges, this plan edits no file that `git status`
   showed modified on 2026-11-07 and no RADIO.md section.
