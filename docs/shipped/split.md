# Split — the station (origin) apart from the site (edges)

> *Formerly `SPLIT_PLAN.md`.*

**Status: shipped** (Steps 1–10, complete 2026-11-08).

The station process got two listeners (public and admin), origin cache
headers, trusted-proxy client IPs, an off-air client state, and an nginx
edge + allowlisting gateway reached over Tailscale. Normative behaviour: RADIO
§3, §9.1, §11, §12, §14. The operator side is `pilgrim/deploy/README.md` and
`pilgrim/deploy/RUNBOOK.md`; the Fargate web tier that takes over the edge's
job is docs/pending/scalability.md.

Code: `pilgrim/server.py` (`create_app` public/admin), `pilgrim/deploy/edge/`,
`gateway/`, `tailscale/`, `mac/origin.sh`, `pilgrim/tools/render_deploy.py`,
`split_smoke.py`, `make deploy-check | split-smoke | e2e-split`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/split.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.1 The target, in one picture

```
                         ┌── EDGE (EC2, many, stateless) ───────────────────┐
Browser ─▶ Cloudflare ─▶ │ nginx: serves pilgrim/web/ (static site)          │
           (proxy)       │        proxies /api/* to the origin, caches media │
                         └───────────────┬───────────────────────────────────┘
                                         │ one TCP port (Tailscale in prod,
                                         │ loopback in this plan)
                         ┌── GATEWAY (container on the M5) ─▼────────────────┐
                         │ nginx: path + method ALLOWLIST, rate limits       │
                         └───────────────┬───────────────────────────────────┘
                                         │ http, local only
                         ┌── ORIGIN (M5 Mac Studio, exactly one) ─▼──────────┐
                         │ pilgrim.server  PUBLIC listener  (port 5000)      │
                         │                 ADMIN listener   (port 5001, lo)  │
                         │ scheduler + producer + store + library/ + db      │
                         └───────────────────────────────────────────────────┘
```

### 0.2 Facts you must not get wrong

1. **There is no continuous audio stream.** The browser fetches the program
   list (`GET /api/station/program`) and separate FLAC files
   (`GET /api/media/{id}`), then joins them with Web Audio (RADIO.md §9.2).
   **Keep this model.** Do not add Icecast, HLS, MP3, Opus or server-side mixing.
2. **There is exactly one scheduler, on the origin.** Edges never run
   `pilgrim.server`, `pilgrim.producer`, or anything that opens `station.db`.
   An edge is nginx plus the `pilgrim/web/` static files. Nothing else.
3. **SQLite stays.** One process writes `station.db`. Do not add Postgres,
   Redis, SQLAlchemy or any replication. Do not change the database schema in
   this plan.
4. **When the origin is unreachable, the station is off the air.** Edges do not
   replay stale programs, loop audio, or fake state. They say "off air"
   (Step 4).
5. **Heartbeats** (`POST /api/station/heartbeat`, every 5 s per playing
   browser) feed the live listener count and the `airplay` log. They do **not**
   drive the playhead. The playhead runs on the origin's clock
   (`scheduler.py:122`). In this plan heartbeats are proxied through unchanged.
6. **Tailscale is not your job.** Do not install, run, or configure Tailscale.
   Do not add Tailscale code to Python. You only write the gateway files in
   Step 6, and the operator runs them. Everything you build and test runs over
   **loopback on this host**.
7. **Cloudflare is not your job.** Do not call the Cloudflare API. Step 9 lists
   what the operator configures.

### 0.3 Rules for this plan (on top of AGENTS.md §1)

- Do not touch the running production nginx (`pilgrim-nginx` container,
  config in `/home/ec2-user/site/deploy/`). It is outside this repo.
- Do not start, stop, or restart the live station on port 5000 unless a step
  says so. Local split tests use **other ports** (§0.4) and a **temp
  library** (e2e server or fakes), never `pilgrim/station.db`.
- Do not use `docker compose down -v`, `docker system prune`, or remove any
  container or image you did not create in this plan.
- All new deploy files live under `pilgrim/deploy/` (AGENTS.md layout rule:
  the repo root holds only repo-level files).
- Placeholders in deploy templates use the form `@@NAME@@`, never `${NAME}`
  or `$NAME`, because nginx itself uses `$name` variables. The renderer
  (Step 5) must fail if any `@@...@@` is left unreplaced.

### 0.4 Ports used in this plan

| What | Production (M5 / EC2) | Local split test (this host) |
|------|----------------------|------------------------------|
| Origin public listener | `127.0.0.1:5000` on the M5 | e2e server on an OS-chosen free port |
| Origin admin listener | `127.0.0.1:5001` on the M5 | not used by e2e |
| Gateway nginx | `127.0.0.1:8090` inside the container, published by Tailscale serve on `:443` | `127.0.0.1:18090` |
| Edge nginx | `:8080` behind TLS on the EC2 | `127.0.0.1:18080` |
| Edge health (for ALB/IaC) | `GET /edge-health` on the edge | same |

Before using 18080 or 18090, check they are free (`ss -ltn`). If one is
taken, pick another free port and say so in your report.

## Deferred (do not build now)

- Edge-side heartbeat aggregation (one summary per edge every 5 s). Needs a
  new origin endpoint and a RADIO.md §11 change.
- An API-version field so edge-served UI and origin API can detect mismatches.
- Pre-warming edge media caches when a song is committed.
- Media in R2 or S3 (push model).
- Opus or other non-FLAC delivery (needs gapless verification first; AGENTS.md
  §10 pitfalls).
