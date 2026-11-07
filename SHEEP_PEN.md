
# Pilgrim Dot Farm — Sheep Pen Chat Feature

> **How to use this document (read first).** This file is the **brief** and
> the starting point for the Sheep Pen work. It is not the plan, and you do
> not rewrite it. Your deliverable is a new file, `SHEEP_PEN_PLAN.md`, at the
> repo root, in the shape of the existing work plans (`VOTING.md`,
> `IDLE.md`): a short "read this first" section, then numbered **Steps**
> each ending with a *Done when* line, the AGENTS.md §7 checks, a commit
> citing RADIO.md sections, and the §8 report; then "Verification commands",
> "Rollback" and "Open questions for the operator". Add one line for it to
> the "Active work plans" paragraph at the top of `AGENTS.md`.
>
> Precedence: `RADIO.md` and `AGENTS.md` first; then §0.6, then §0.7, then
> §0.5, then the rest of this brief. Where this brief says **"operator
> decision"**, do not reopen it. Where it says **"the plan must specify"**,
> decide it yourself and record the choice under a *Decisions* heading in
> the plan. Where §0 states a dated fact, confirm it against the named file
> and, if it has drifted, append the correction to a *Drift log* at the end
> of the plan rather than editing this file. Cite this brief as
> `SHEEP_PEN §x.y`.
>
> Do not implement anything while writing the plan. Do not call AWS; §0.4
> already records the deployed state, and `AWS_MCP_USAGE.md` explains the
> access paths if an implementation step later needs one. Keep Playwright
> MCP use to what §12 describes. If a section of this brief cannot be
> satisfied without breaking an AGENTS.md hard rule, say so in the plan's
> open questions instead of bending the rule.

## Objective

Design and implement a public, real-time chat feature called **The Sheep Pen** for https://pilgrim.farm.

Produce a detailed, phased implementation plan suitable for execution by a local coding agent. Inspect the current repository thoroughly before specifying code changes.

**Audience and scope:** you are the local coding agent on the Mac Studio,
with this checkout, the Terraform checkout and Playwright MCP available.
The current deliverable is the plan (§13), not feature implementation. Your
plan must include the complete release and verification procedure in §14,
so a later authorized implementation ends with a verified deployment and
handoff. The operator has authorized you to build and push the Docker image
as needed during that implementation and use the `ecs_release` rollout
mechanism; do not leave these routine release steps as unexplained manual
work for the operator.

We want an architecture capable of accommodating significant growth, potentially 10,000+ concurrent listeners, rather than a prototype that will require substantial reengineering.

Use the existing AWS infrastructure, local LLM resources, and established repository conventions.

## 0. Verified facts (read before planning; checked against the checkout and terraform on 2026-11-07)

These were verified by inspecting this checkout and `~/projects/terraform-aws-pilgrim/modules/ec2`
(including its `terraform.tfstate`). RADIO.md remains authoritative per
AGENTS.md. Within this planning brief, this section takes precedence over
§1 and older planning assumptions. Confirm the named implementation before
using these dated facts; report drift or a conflict with RADIO.md rather
than silently overriding the specification. Do cite these facts.

### 0.1 Where code lives and what already exists

| Need | What exists today | File |
|------|-------------------|------|
| Web tier entry point | `WebStation(PublicSurface)`; `create_web_app()` reuses `server._add_public_routes` unchanged, then `_add_api_404`, then the static mount | `pilgrim/web_app.py` |
| Public route registration | `_add_public_routes(app, station, health)`; every public route lives here and is shared by the Mac and Fargate | `pilgrim/server.py:1052` |
| Admin routes | `_add_admin_routes` on the Mac's **admin listener only** (`station.admin_host:admin_port`, loopback `127.0.0.1:5081`); operator auth is `Depends(require_dj)` = bearer `PILGRIM_DJ_TOKEN`. The Fargate web tier has **no** admin routes and answers 404 for `/api/admin/*` | `pilgrim/server.py:1373`, `pilgrim/dj_security.py` |
| Operator console | Textual TUI talking to the admin listener; bindings in `BINDINGS`, HTTP in `tui/client.py` | `pilgrim/tui/app.py`, `pilgrim/tui/client.py` |
| Canonical client IP | `PublicSurface._client_ip(request)` + `_canonical_ip`: trusted peer (`visitors.trusted_proxies`) → `CF-Connecting-IP`, else `X-Real-IP`, else first `X-Forwarded-For`; untrusted peer → socket address. Container overlay sets `trusted_proxies: ["172.31.0.0/16"]` (the ALB). It takes a `Request`; a `WebSocket` has the same `.client`/`.headers`, so widen the type hint to `starlette.requests.HTTPConnection` rather than copying the logic | `pilgrim/server.py:141`, `pilgrim/deploy/container/overlay.yaml` |
| Browser identity | `voter.js` keeps one random 32-hex id in `localStorage` (`pd_voter`), exposed as `window.PD_VOTER.id()`; server pattern `VOTER_PATTERN = ^[0-9a-f]{32}$`. Reuse it as the chat browser token | `pilgrim/web/voter.js`, `pilgrim/voting.py` |
| Hot state in Valkey | `HotState` over a **sync** `redis.Redis` client (`hotstate.connect(url, cfg)`), keys prefixed `cache.key_prefix` (`pilgrim:`), JSON values, single-key commands only. Nothing async, no Pub/Sub, no Streams yet | `pilgrim/hotstate.py` |
| Redis fake | `FakeRedis` implements only `ping/get/set/getdel/delete/scan_iter/pipeline(transaction=False)` and raises CROSSSLOT on multi-key ops. **No streams, pub/sub, INCR, EXPIRE, hashes, or async API.** The plan must extend it (or add `FakeAsyncRedis`) before any chat test can run | `pilgrim/tests/fakes/fake_redis.py` |
| LLM client | `LLM.chat_json(model, system, user, max_tokens)` / `chat_text`; SSE streaming; `temperature` is hardcoded `0.8` (add a parameter for moderation). No tool-calling / function-calling code path exists anywhere in the repo | `pilgrim/pipelines/llm.py` |
| Request moderation (reference pattern) | `Moderation.moderate(text)`: system = template only, untrusted text JSON-escaped into the user turn as `<request>…</request>` DATA, strict boolean check, retry once, **fail closed** | `pilgrim/pipelines/moderation.py`, `pilgrim/prompts/moderation.md` |
| Deterministic prefilter (reference) | `prefilter(text)`: http/www, bare domains with a TLD list, spelled-out "dot com", emails, 7+ digit phone runs, `@handles` | `pilgrim/pipelines/request_filter.py` |
| Rate limiting today | `_make_rate_limiter` is an **in-memory per-process** deque keyed on canonical IP. It does not survive scale-out (SCALABILITY.md Step 5 notes this as a known gap) | `pilgrim/server.py:971` |
| Logging | `logging_setup.py` emits **text** `key=value` lines, not JSON. The container's stdout is shipped by the `awslogs` driver to log group `/aws/ecs/pg-app/app` (retention 7 days, hardcoded in `ecs.tf`). A JSON formatter does not exist yet | `pilgrim/logging_setup.py` |
| Store / schema | One engine-neutral `Store` (`CREATE TABLE IF NOT EXISTS …` in SQLite flavour, rewritten for Postgres by `PostgresConn`). New tables go in `Store`'s schema block; never branch on engine | `pilgrim/store.py:203`, `pilgrim/db.py` |
| Config | `pilgrim/config.yaml` + pydantic models in `pilgrim/config.py`; container overlay deep-merges `deploy/container/overlay.yaml`. A new `chat:` block needs a `Chat` model and a `Config.chat` field | `pilgrim/config.py` |
| Fakes for tests | `FakeLLM`, `FakeKokoro` in `pilgrim/tests/fakes/__init__.py`; `FakeRedis`; e2e server with fakes in `pilgrim/tests/e2e/e2e_server.py` (Playwright viewport 480×400, `workers: 1`) | `pilgrim/tests/` |
| Frontend | `index.html` sections in order: `radio`, `requests`, `hit`, `motivation`, `infra`, `credits`; scripts loaded with `?v=YYYYMMDDx` cache-busters. Insert `<section class="window sheep" data-win="sheep">` between `requests` and `hit`, and a `chat.js` | `pilgrim/web/index.html` |

### 0.2 Giles, as he actually exists

- He is the **field reporter** (`field_report` item type, RADIO §4). Name comes
  from `talk.field_reporter_name` (config.yaml line 304, default `Giles`,
  still marked PLACEHOLDER). Voice `bm_lewis`, copy model
  `models.field_report: qwen38` (not Ornith).
- Persona lives in `pilgrim/prompts/field.md` ("very British, very earnest
  correspondent who files his reports while standing in a literal field …
  dry, polite, faintly bewildered"). Banned there: real politics, real news,
  profanity, real brand names, breaking the fourth wall.
- His canon sign-off is enforced **in code**, not just prompted
  (`VoicePipeline.CATCH_PHRASE` in `pilgrim/pipelines/voice.py`): `"This is
  {name} signing off with a reminder that I'm outstanding in my field."` An
  opener such as "Giles here, outstanding in my field." is not canon and must
  not appear; §8's examples are written in canon form. **Operator decision (2026-11-07): every chat dispatch ends with the
  canon sign-off sentence, verbatim, exactly once.** Known regression to
  avoid: the original `field.md` told the model to "slip the pun in
  naturally once in the body" *and* to close with the sign-off, so Giles
  said "outstanding in my field" twice per report. Commit `63a32ac` removed
  the body instruction; the only mention left in the prompt is the sign-off
  itself. `voice.py` still guards only the *missing* case (`CATCH_PHRASE not
  in text.lower()` → append). The chat worker must:
  1. derive `prompts/field_chat.md` from the **current** `field.md` wording
     (no "pun in the body" line; the phrase appears once, in the sign-off
     instruction);
  2. enforce **exactly one** occurrence in code: count
     `"outstanding in my field"` case-insensitively; if 0, append the canon
     sentence; if ≥ 1, remove every sentence containing it from the body and
     append the canon sentence once;
  3. test all three fake outputs (none, once, twice) and assert the final
     text contains the phrase exactly once and ends with the canon sentence
     (mirror `test_sfx.py::test_field_report_voice_and_catch_phrase`).
- He is produced by the Mac's producer (`pilgrim/producer.py`) for air; chat
  dispatches are text-only and must be a **separate worker**, never a change
  to the producer or scheduler (hard rule 1).
- The DJ is Rosie Thistlebottom (`pilgrim/bible/dj_persona.md`); station
  canon lives under `pilgrim/bible/`. Do not invent new characters.

### 0.3 LLMs reachable through LiteLLM (`GET /model/info`, probed 2026-11-07)

| Model | Facts |
|-------|-------|
| `Ornith-1.5-9B` | GGUF Q5_K_M on the 4070 Ti host via LiteLLM. **Reasoning model** (emits `reasoning_content`; docs/backends.md §1). LiteLLM reports `supports_function_calling: None` (unknown, not advertised). No `context_tokens` entry in config, so `LLM.room_for` returns None and `max_tokens` is omitted → server default cap 8192, fine for a short JSON verdict. Used today for commercials and liners only |
| `qwen38` | Reasoning model, `supports_function_calling: True`, 131k in/out. Request-line moderator and Giles's copy model |
| `deepseek-v4-flash` | Present in the model list (`mode: chat`). Not used by the station |

Consequence: **there is no tool-calling path for Ornith, and the repo's
contract with every LLM is "one `chat_json` call in, one strictly validated
JSON object out, code executes" (AGENTS §5).** §6 Layer B below is written to
that contract. The moderation worker reaches LiteLLM directly from the Mac
(`hosts.litellm`); from Fargate it would have to go through the bastion relay
`172.31.35.80:4000` (overlay), which exists but adds a hop.

### 0.4 Infrastructure as deployed (terraform `modules/ec2`, state inspected)

- **Path:** Cloudflare (proxied DNS, SG allows Cloudflare CIDRs only) → ALB
  `pg-alb` :443 (`idle_timeout = 300`) → target group `pg-http` (HTTP :8080,
  `target_type ip`, health `/api/health` every 15 s, `deregistration_delay =
  30`) → ECS Fargate service `pg-app` (`desired_count = var.ecs_count` = **1**,
  `enable_autoscaling = false`, cpu 1024 / mem 2048, ARM64, platform 1.4.0,
  private subnets with NAT egress, `enable_execute_command = false`). The EC2
  nginx edge in `pilgrim/deploy/edge/` is **not** in this path and has no
  WebSocket `Upgrade` handling; do not modify it. SCALABILITY.md Step 9
  retires it.
- **WebSocket implications:** ALB passes WebSockets on HTTP/HTTPS listeners
  with no config change. The ALB idle timeout is 300 s and Cloudflare closes
  idle WebSockets at ~100 s, so the server must ping at ≤ 60 s. A deploy or
  scale-in cuts every socket on the old task 30 s after deregistration; the
  client must reconnect with jitter and replay from its last stream id.
  There is one task today, so multi-task fan-out is untested in production
  until `ecs_count` > 1 or autoscaling is added (a terraform change).
- **Valkey:** `aws_elasticache_serverless_cache` engine `valkey` 9, TLS only,
  no AUTH, `data_storage.maximum = 5 GB`, **`ecpu_per_second.maximum =
  1000`**. Serverless is cluster mode behind one endpoint; multi-key
  commands across slots fail with CROSSSLOT (docs/backends.md §12). Per the
  AWS supported-commands page for serverless caches: Streams (`XADD`,
  `XRANGE`, `XREVRANGE`, `XREAD`, `XTRIM`, consumer groups) are supported;
  `PUBLISH`/`SUBSCRIBE` and `SPUBLISH`/`SSUBSCRIBE` are supported and
  "internally use sharded PUBSUB"; **`PSUBSCRIBE`/`PUNSUBSCRIBE` are
  unavailable** (use exact channel names, never patterns); `EVAL`/`EVALSHA`
  are supported (keep every script single-key or hash-tagged `{chat}`);
  `KEYS`, `CLIENT LIST/ID`, `FUNCTION`, `WAIT` are unavailable. Subscriptions
  do not survive a cache failover or scale event; the per-task subscriber
  must reconnect and resubscribe, and clients must be able to resync from
  the stream.
- **ECPU budget is the real ceiling, not sockets.** Roughly one ECPU per
  simple command per KB. Today's heartbeat path already costs one `SET` per
  listener every 5 s, so 10,000 listeners ≈ 2,000 commands/s, double the
  configured cap, before a single chat message. Chat adds per message: ban
  checks, two rate-limit `INCR`s, one `XADD`, one `PUBLISH` (fan-out to tasks
  is not multiplied per browser). **For this release the cap stays at
  1000 and is not a gate** (operator decision, 2026-11-07; it is an
  ElastiCache knob, not an ECS one, and today's listener counts are far
  below it). It is deferred with fleet sizing (§0.6.7): when the operator
  later scales out, `ecpu_per_second.maximum` is raised alongside
  `ecs_count`, sized from measured commands per listener. The plan's only
  obligation now is to keep per-message command counts small and to expose
  the Valkey command-rate metric so that later sizing has numbers.
- **IAM, as it stands (this is the gap the CloudWatch work was meant to close):**
  - The deployed Fargate **task role** is the module-created `pg-app-tasks-*`
    with exactly one customer policy: `secretsmanager:GetSecretValue /
    DescribeSecret` on the DB and Stripe secrets (`tasks_iam_role_statements`
    in `ecs.tf`). It has **no CloudWatch Logs permission**.
  - The `logs:*` on `*` policy (`pilgrim-logs-read-write`) is attached to
    `aws_iam_role.pg` = `pg-s3-read`, whose trust policy is
    `ec2.amazonaws.com`. `ecs.tf` passes it as `iam_role_arn`, which in the
    `terraform-aws-modules/ecs` service module is the ECS **service** role
    (unused with `awsvpc` networking), not the task role. So this grant
    reaches the EC2 bastion instance profile only.
  - The module-created **execution role** `pg-app-task-exec-*` has
    `logs:CreateLogStream` + `logs:PutLogEvents` on `*`: that is what ships
    container stdout to `/aws/ecs/pg-app/app`. Application code cannot use
    the execution role.
  - IAM user `mac-studio` (the Mac's `media.aws_profile`, the only profile in
    `~/.aws` on the Mac) has S3 read/write, DB-secret read and, since
    2026-11-07, inline policy `mac-studio-logs-read-write` = `logs:*` on `*`
    (live IAM, verified). The Mac-side worker therefore builds its CloudWatch
    client from `media.aws_profile`, exactly as `media_publish.py` does for
    S3; no new profile or config key is needed. (`kenny-logger` also holds
    `logs:*`; it is not used by the station.) A missing/denied credential is
    a logged degradation, never a crash.
  - Required fix for the plan: add a `logs:CreateLogStream`,
    `logs:PutLogEvents`, `logs:DescribeLogStreams` statement scoped to the
    chat log group ARN(s) to `tasks_iam_role_statements` in `ecs.tf`
    (the Mac side is already covered by `mac-studio`). Create the
    log group(s) in
    terraform with a `retention_in_days` **variable** (default `7`, §0.6.4;
    today the only retention knob is the hardcoded `7` in `ecs.tf`). Prefer scoping
    `pilgrim-logs-read-write` down from `logs:*` on `*` at the same time.
- **Secrets:** `REDIS_URL`, `DATABASE_URL`, `DB_PASS_SECRET_ARN`,
  `STRIPE_SECRET_ARN` are plain task environment (`env.tf`); `LITELLM_TOKEN`,
  `BRAVE_SEARCH_KEY`, `PAID_OVERRIDE_TOKEN` are Secrets Manager references.
  The Mac reads the same `REDIS_URL`/`DATABASE_URL` from `.env` over the
  bastion's tailnet route (~250–350 ms per Valkey round trip from the Mac,
  single-digit ms inside the VPC; docs/backends.md §12). Place latency-
  sensitive chat paths (socket handling, Layer A, rate limits) on Fargate,
  and the LLM-bound workers (Layer B, Giles) on the Mac.
- **Cloudflare** is operator-managed outside this repo (deploy README:
  "This repo does not call the Cloudflare API"). WebSockets are on by default
  for proxied hostnames; nothing to change, but note the 100 s idle limit.

### 0.5 Decisions this section settles (so the planner does not reopen them)

1. **Topology:** sockets, Layer A, rate limits, history replay and the
   per-task Pub/Sub subscriber run in the Fargate web tier
   (`pilgrim/web_app.py` + a new `pilgrim/chat/` package). Layer B (Ornith)
   and Giles run in one new Mac-side process, `python -m pilgrim.chat_worker`,
   installed as its own launchd unit beside the station (pattern:
   `pilgrim/deploy/mac/origin.sh`), **not** inside `server.py`'s station
   process and never touching `scheduler.py`/`producer.py`/`selector.py`.
2. **Admin plane:** `/api/admin/chat/*` routes are added to
   `_add_admin_routes` on the Mac's loopback admin listener behind
   `require_dj`, plus TUI bindings. They act by writing to Valkey (events
   stream + `PUBLISH`), which every Fargate task observes. The web tier gets
   no admin routes.
3. **Redis client:** chat uses `redis.asyncio.Redis.from_url(url,
   socket_timeout=cfg.cache.timeout_s, …)` (same URL/config as
   `hotstate.connect`, same `cache.key_prefix`), one client + one `pubsub()`
   per task. `HotState` stays sync and untouched.
4. **Durability split:** Valkey holds everything hot (streams with
   `MAXLEN`, sessions, rate limits, live ban keys with TTL). Postgres holds
   the audit copy and anything that must survive the cache: `chat_users`,
   `chat_messages` (one row per accepted message, text nulled after
   `chat.retention_days`), `chat_bans`, `chat_moderation` (new `Store`
   tables; RADIO §14 updated in the same change per hard rule 8). Both tiers
   reach the same Postgres: the Fargate task through `DATABASE_URL` in its
   environment, the Mac worker through `DATABASE_URL`/`DB_PASS_SECRET_ARN`
   in the repo-root `.env` (present today) via `db.open_store(cfg)`.
5. **Logging:** add a JSON formatter to `logging_setup.py` selected by a new
   `logging.format: text|json` key (container overlay → `json`), so ordinary
   events reach `/aws/ecs/pg-app/app` as JSON for Logs Insights. Message
   text and IP hashes (§0.6.5; never raw IPs) go **only** to a dedicated audit log group written directly
   with boto3 `PutLogEvents` by whichever process acts (web tier for
   accept/reject, worker for moderation), never to stdout. Metrics are
   emitted as CloudWatch **Embedded Metric Format** lines on stdout (no
   extra IAM; CloudWatch extracts them from the log group).

### 0.6 Operator decisions, second pass (2026-11-07)

These settle the questions §0.5 left open. They amend §4, §6 Layer A, §7, §9
and §11 below; where the two disagree, this section wins.

1. **Display names are unique.** Non-unique names were judged dangerous
   (impersonation by coincidence). One active user per normalized name:
   NFKC → casefold → collapse whitespace is the uniqueness key. First claim
   wins; the loser gets a 409 and must pick another. The authoritative check
   is a partial unique index in Postgres (`chat_users.name_norm` where
   `released_at IS NULL`); `pilgrim:{chat}:name:{name_norm}` (`SET NX EX`) is
   the cheap pre-check, never the authority. A name is released after
   `chat.name_release_days` (default 30) of inactivity, by the worker, or by
   an admin. Changing to a free name keeps the same user id, so history
   presentation updates without rewriting messages. Station-character and
   operator names are reserved (obscured like §0.6.2).
2. **Prohibited terms never appear as plain text in the repo.** The
   deterministic banned-term list ships as SHA-256 hex digests of normalized
   terms in `pilgrim/chat/prohibited.py` (tokens and multi-word phrases),
   matched by hashing the normalized candidate — never by substring. Adding
   a term is an operator step that feeds terms on stdin to a small local
   script; the terms are never committed, logged, echoed in an error, or put
   in a prompt. Reserved display names use the same mechanism. Accepted
   limitation: hashed matching is token-exact, so leetspeak/homoglyph
   evasions fall to Layer B, and a dictionary attack can recover the list
   (the goal is no plaintext in the repo and logs, not secrecy).
3. **No profanity filtering and no masking.** Swear words are not blocked,
   not censored, not flagged, and are rendered exactly as typed. There is no
   profanity list anywhere in the repo (the same obscuring rule applies
   vacuously). `profanity_mild` is removed from the Layer A flag vocabulary;
   Layer B may still act on hate, harassment and threats.
4. **Retention is 7 days.** The chat audit log group is created with
   `retention_in_days = 7` (a terraform variable, default 7), and message
   text — accepted, rejected, deleted or moderated — is purged from Postgres
   after 7 days by a daily worker sweep that nulls `chat_moderation.text` and
   the audit copy while keeping the tombstone and decision rows. CloudWatch
   audit lines age out with the group. Nothing about message content is kept
   beyond 7 days.
5. **IP storage: plain SHA-256.** `ip_hash = sha256(canonical_ip).hexdigest()[:16]`,
   no secret, no HMAC, no config key. It is a pseudonym for rate limits and
   ban keys only; the raw IP is never stored, logged or shown. The operator
   does not require reversibility.
6. **Key naming is ours to choose.** Every chat key is
   `pilgrim:{chat}:<name>` — the `{chat}` hash tag is part of the key body so
   a Lua script or any multi-key command stays in one slot (serverless
   cluster mode, §0.4). The illustrative names in §9 are amended accordingly.
7. **Scaling is deferred and out of scope for this release.**
   `ecs_count` stays `1`, autoscaling stays `false`, and the Valkey
   `ecpu_per_second.maximum` stays `1000`; the operator will size the fleet
   and the cache later, together. The architecture must therefore remain
   stateless-per-task and sticky-free so that scaling is a terraform-only
   change (§11), but no capacity, autoscaling or load-test work is part of
   this implementation. Report measured single-task limits honestly rather
   than claiming 10,000 listeners.

### 0.7 Settled mechanics (so the plan refines these rather than inventing them)

The brief elsewhere says "the plan must specify" for several mechanisms. A
smaller planner does better refining a fixed skeleton, so these are fixed.
Field types, error codes and edge cases are the plan's to fill in.

1. **Dependencies already present; add none.** `uvicorn[standard]` brings
   `websockets` 17.4, so FastAPI `@app.websocket` works in the web image as
   is. `redis==8.1.0` provides `redis.asyncio.Redis` with `pubsub()`,
   `xadd`/`xrange`/`xrevrange`/`xtrim`, `incr`, `expire`, hashes and sets.
   `psycopg` and `boto3` are pinned. Any new line in `requirements.txt`
   needs a reason in the plan.
2. **Endpoints.** WebSocket at `/api/chat/ws` (registered in a new
   `pilgrim/chat/routes.py` and mounted from `web_app.create_web_app` and
   the e2e server **before** `_add_api_404`; the 404 catch-all is HTTP-only
   and does not affect the socket route). REST: `POST /api/chat/name`
   (claim/change a name; 409 when taken), `GET /api/chat/history?after=<id>`
   (cold load and gap fill, same shape as the `message` frame, tombstones
   applied). Both return `Cache-Control: no-store`. The Mac station process
   serves none of these; it serves `/api/admin/chat/*` only (§0.5.2).
3. **Accept path (one message), in this order.**
   1. Layer A (§6) on the trimmed text; any failure → `error` frame, nothing
      written.
   2. Ban check: `GET pilgrim:{chat}:ban:user:{id}` and `…:ban:ip:{ip_hash}`.
   3. Rate limits: one single-key Lua `EVAL` per key (`INCR` + `EXPIRE` on
      first hit) for `rate:user:{id}` and `rate:ip:{ip_hash}`; also honour
      `pilgrim:{chat}:slow_mode` if set.
   4. `XADD pilgrim:{chat}:messages MAXLEN ~ chat.history_max *` with fields
      `{user_id, name, text, ts, client_msg_id, flags}`. The returned stream
      id **is** the message id everywhere (frames, Postgres, logs).
   5. `INSERT chat_messages (id=<stream id>, user_id, name, text, ts,
      client_msg_id, status='pending_review')`. If this insert fails: `XDEL`
      the entry, return `error {code: "retry"}`; the message was not sent.
   6. `PUBLISH pilgrim:{chat}:pubsub <message frame JSON>`. If publish
      fails, log it and continue: the message is durable and every task's
      resync (item 4) delivers it within `chat.resync_s`.
   7. Audit line to the chat log group; ack the sender with
      `{client_msg_id, id}`.
   `client_msg_id` (a client-generated UUID) makes resends idempotent: the
   accept path first checks `pilgrim:{chat}:sent:{user_id}:{client_msg_id}`
   (`SET NX EX 300` holding the stream id) and returns the existing id on a
   repeat.
4. **Delivery and gap recovery.** Each task runs one subscriber task on
   `SUBSCRIBE pilgrim:{chat}:pubsub` (exact name) and fans frames out to a
   bounded `asyncio.Queue(maxsize=chat.client_queue_max)` per socket; a full
   queue closes that socket with code 1013 and the client reconnects and
   replays. The subscriber also remembers the last stream id it delivered
   and every `chat.resync_s` (default 5) runs `XRANGE (last_id +`; anything
   it finds was missed by Pub/Sub and is delivered the same way. On a
   subscriber error it reconnects with backoff and resubscribes (serverless
   drops subscriptions on failover). This one loop covers publish failures,
   Pub/Sub gaps and cache failover without a second mechanism.
5. **Tombstones.** A deletion writes three things: `UPDATE chat_messages SET
   deleted_at, deleted_by` (durable truth), `SADD pilgrim:{chat}:deleted
   <id>` (one key, one slot; the fast filter), and the `message_deleted`
   event (live propagation). Every path that exposes text (history replay,
   the welcome snapshot, Layer B's window, Giles's context) filters through
   `SMISMEMBER pilgrim:{chat}:deleted` first and falls back to Postgres if
   the set key is missing. The worker prunes set members older than the
   oldest stream entry once a day, so the set stays bounded by
   `chat.history_max`.
6. **Frames.** JSON text frames, one object each, `type` first. Client →
   server: `hello {token, last_id?, client_version}`, `name {name}`, `send
   {client_msg_id, text}`, `ping`. Server → client: `welcome {name?, banned_until?,
   slow_mode?, last_id, history: [message…]}`, `message {id, name, text,
   ts, giles: bool}`, `ack {client_msg_id, id}`, `deleted {id}`, `renamed
   {user_pseudonym, name}` (optional; names may instead be re-read on the
   next history load), `warned {reason}`, `banned {until}`, `slow_mode
   {interval_s}`, `error {code, detail}`, `pong`. No frame ever carries a
   user id, session hash, IP or IP hash. The server pings every
   `chat.ping_s` (default 30 ≤ the 100 s Cloudflare idle limit) and closes
   after two missed pongs.
7. **Shutdown.** On SIGTERM (ECS stop, 30 s `stopTimeout`) the web tier
   stops accepting sockets, sends close code 1012 to every client, and
   exits; clients reconnect with jitter (`chat.reconnect_min_s`…`max_s`) and
   replay from `last_id`. The Mac worker finishes the batch in hand before
   exiting so the cursor is never left mid-batch.
8. **Operator switches** are keys the admin routes set and every task
   reads: `pilgrim:{chat}:posting_disabled` (TTL optional) and
   `pilgrim:{chat}:giles:paused`. Add both to §9's list.
9. **Retention knobs.** One application key, `chat.retention_days` (default
   7; the purge sweep in the worker), and one terraform variable,
   `chat_log_retention_days` (default 7; the audit log group). The plan
   keeps them equal and says so in RADIO §12.
10. **Giles's model** is `models.chat_giles`, default `qwen38` (the same
    model as `models.field_report`; §0.2). §8's "consider a different
    model" is settled: no.

## 1. Existing architecture

Repository:
https://github.com/pilgrim-dot-farm/pilgrim-dot-farm

The following were verified through a brief inspection of the public repository:

- `AGENTS.md` establishes mandatory development rules.
- `RADIO.md` is the authoritative station specification.
- `SCALABILITY.md` documents the migration to ECS Fargate, RDS PostgreSQL, and ElastiCache Valkey.
- `pilgrim/web_app.py` is the Fargate web entry point.
- `pilgrim/hotstate.py` provides existing Valkey-backed state.
- `pilgrim/config.yaml` governs backend addresses, models, and configuration.
- The application uses Python, FastAPI, asyncio, and a plain HTML/CSS/JavaScript frontend.
- The existing voting system uses browser-generated identity combined with canonical client IP, without user accounts.
- The station has existing tests, fake backends, and Playwright browser testing.

The infrastructure is:

    Browsers
       |
    Cloudflare
       |
    AWS ALB
       |
    ECS Fargate web service (one task today; designed for multiple tasks)
       |                 |
    ElastiCache Valkey   RDS PostgreSQL

    Mac Studio / local inference hosts
       |
    Tailscale
       |
    EC2 bastion / VPC subnet router
       |
    Valkey / PostgreSQL

The local model infrastructure includes:

- Ornith 1.5 9B on an RTX 4070 Ti, accessible through LiteLLM.
- Qwen 3.8 27B for more capable DJ/news generation.
- Existing station announcers and characters, including Giles.

`AGENTS.md` identifies Ornith as an existing model used for commercials and liners. Extend this integration rather than creating an entirely separate inference stack.

The public repository documents an evolving infrastructure migration. Inspect the current checkout and treat its implementation as authoritative over older documentation.

## 2. Chat placement and presentation

Add a new section titled **THE SHEEP PEN**.

Page order:

1. Existing radio player and station content
2. Song requests and request voting
3. THE SHEEP PEN
4. Hit counter
5. Social links/footer

Preserve the existing aesthetic:

- Dark, retrofuturistic barnyard theme.
- Neon accents and existing gradients.
- Mobile-first layout.
- No separate page required.
- No frontend framework or build system.
- No interference with radio playback or request functionality.

The chat should display:

- Messages in chronological order.
- User-selected display names (unique, §4; no duplicate active names).
- Clearly distinguished messages from Giles.
- A name-selection interface.
- A message composition field.
- Character counter.
- Send button.
- Connection/reconnection status.

A user who has not selected a valid name may read chat but cannot send messages.

The input should be disabled until a name is registered.

Support switching names without losing the underlying user identity.

Do not expose IP addresses, internal user IDs, or moderation metadata publicly.

Do not make Giles look like an ordinary human participant. He is an explicitly identified station character.

## 3. Real-time architecture

**Use WebSockets and ElastiCache Valkey Pub/Sub from the beginning.**

Do not implement periodic polling as the primary delivery mechanism.

Design:

    Browser
       |
       | WSS
       v
    Cloudflare -> ALB
       |
       v
    ECS Fargate WebSocket endpoints
       |
       | Redis Pub/Sub
       v
    ElastiCache Valkey
       |
       +-- Recent chat history
       +-- Connected user/session state
       +-- Rate limiting
       +-- Bans
       +-- Moderation queue
       +-- Giles announcements

Every ECS task maintains WebSocket connections for its attached clients.

Each ECS task subscribes to the shared Redis Pub/Sub channel and distributes events to locally connected clients.

There should be one logical subscription per ECS task, not one Redis subscription per connected browser.

Support horizontal scaling without sticky sessions.

Use Redis Streams for message history and moderation tracking.

Pub/Sub provides live delivery; Streams provide recovery and replay.

Messages should be validated and persisted before notification.

Account for the possibility that a message is persisted but publication fails. Design idempotent recovery or a reliable publisher mechanism.

Message IDs must support deduplication and reconnection.

On reconnect:

- Reestablish identity.
- Check current ban status.
- Retrieve messages missed since the last received ID.
- Recover from gaps in Pub/Sub delivery.
- Synchronize deletions and administrative events.

Consider using a bounded recent-history Stream with a separate durable moderation cursor/queue if trimming could otherwise discard unreviewed content.

Sharded Pub/Sub: settled in §0.4. Serverless runs sharded Pub/Sub
internally; use plain `PUBLISH`/`SUBSCRIBE` on the exact channel name and
never `PSUBSCRIBE`. Gap recovery is §0.7.4.

Avoid unbounded queues and memory growth on slow clients.

Cloudflare, ALB idle timeout, draining and ECS deployment behaviour are
recorded in §0.4; ping cadence and shutdown handling are fixed in §0.7.6–7.
Do not re-investigate; cite them.

## 4. User identity

No accounts, passwords, email addresses, or registration.

Users choose a display name.

Suggested rules:

- 3–24 characters.
- Unicode-aware validation where practical.
- No control characters or invisible formatting tricks.
- No HTML.
- No impersonation of station characters or administrators.
- Reject obvious prohibited terms using deterministic validation.
- No LLM required for username validation.

Persist an anonymous browser token and associate it with a server-side user identity.

Reuse the existing voting identity approach where sensible, but do not assume browser identity alone is trustworthy.

Associate users with the canonical client IP as determined by trusted Cloudflare/proxy headers.

Do not trust arbitrary client-provided forwarding headers.

Allow administrative username changes.

Store messages against a stable user ID, so changing a name updates its presentation without rewriting every message.

Consider sensible limits on name changes to prevent evasion and impersonation.

**Names are unique** (§0.6.1). The uniqueness key is the NFKC → casefold →
whitespace-collapsed form of the display name; the display name itself is
shown as typed. One active user per name, first claim wins, 409 on a taken
name with a nonrevealing message. The authoritative record is Postgres
(`chat_users.name_norm`, partial unique index where `released_at IS NULL`);
`pilgrim:{chat}:name:{name_norm}` with `SET NX EX` is only a pre-check.
Inactivity release after `chat.name_release_days` (default 30), or an admin
release. Reserved names (Giles, the DJ, operator names) are matched through
the obscured list in §0.6.2, never as literals in code. Changing to a free
name keeps the user id, so existing messages re-render with the new name
without rewriting the stream.

## 5. Message rules

Maximum message length: **200 characters**.

Enforce limits on both client and server.

Suggested validation:

- Nonempty after trimming.
- Unicode-aware length enforcement.
- Reject control characters.
- Plain text only.
- No HTML formatting.
- No Markdown-rendered links.
- No hyperlinks or domain-based link evasion.
- No embedded images, media, or attachments.
- No user-submitted image prompts.

Render all user content using safe text APIs, never unsanitized HTML.

Implement URL detection beyond checking only `http://` and `https://`.

Include common domain forms, obfuscated links, and Unicode normalization where appropriate.

Avoid making the filter so broad that normal conversation becomes unusable.

Rate limits should initially be conservative and configurable:

- Approximately one message every three seconds per user.
- Burst limiting.
- Repeated-message detection.
- Per-IP and per-identity limits.
- Connection establishment limits.

Enforce rate limits centrally in Valkey so multiple ECS tasks cannot independently bypass them.

Return clear, nonrevealing error messages.

## 6. Moderation

Moderation has two layers.

### Layer A: Immediate deterministic checks

Run synchronously before accepting messages.

Reject:

- Prohibited extreme slurs, matched against the **obscured digest list** in
  `pilgrim/chat/prohibited.py` (§0.6.2) — token-exact on the normalized form,
  never a plaintext literal in the repo.
- Obvious harassment patterns covered by existing rules.
- URLs.
- Spam/flooding.
- Invalid Unicode or formatting.
- Overlength messages.
- Banned users/IPs.
- Reserved display names (station characters, operators) — same obscured
  digest list.

Reuse the station's established moderation policies where appropriate, without assuming song-request moderation and chat moderation have identical requirements.

**Profanity is not filtered, flagged or masked** (§0.6.3): swear words are
accepted and rendered exactly as typed. There is no profanity list in the
repo and no `profanity_mild` flag. Layer A never bans for language; Layer B
handles hate, harassment and threats, and a ban still requires the §B.3
confidence/severity gate.

### Layer B: Ornith 1.5 9B, the review worker

Every `chat.review_interval_s` (default 300) the Mac-side worker
(`pilgrim/chat_worker.py`, §0.5) reviews what Layer A let through.

**Ornith has no tools.** The repo's LLM contract is one `LLM.chat_json()` call
with a system template from `prompts/` and untrusted data JSON-escaped into
the user turn, returning one strictly validated JSON object (see
`pipelines/moderation.py`; §0.3). LiteLLM does not advertise function calling
for `Ornith-1.5-9B`, and nothing in the codebase calls tools. So "what
tools Ornith needs" splits into three things that **code** does around the
call. Specify all three; none of them is a model-invoked function.

#### B.1 Read side: what the worker gathers and puts in the prompt

Assembled by code from Valkey/Postgres before each call. Each is a field of
the user turn, JSON-encoded, labelled DATA.

| Input | Source | Shape / limits |
|-------|--------|----------------|
| Review window | `XRANGE pilgrim:{chat}:messages (cursor +` in batches of ≤ `chat.review_batch` (default 40) | `[{id, author, name, text, ts, flags}]`, chronological. `author` is a short per-run pseudonym (`u1`, `u2`…) mapped back by code; **never** the real user id, session hash or IP |
| Context tail | the `chat.review_context` (default 15) messages before the window | same shape, marked `context: true`; decisions on these are ignored |
| Author dossiers | `pilgrim:{chat}:user:{id}` hash + Postgres `chat_bans`/`chat_moderation` | per pseudonym: `{messages_24h, warnings_30d, bans_30d, name_changes_24h, first_seen_age_h, currently_banned}` |
| Layer A flags | computed at accept time, stored on the stream entry | e.g. `repeat`, `near_limit_rate`, `name_recent` (no language flags: §0.6.3) |
| Station policy | `prompts/chat_moderation.md` (system turn) | the one published standard, same wording as `prompts/moderation.md`: "Keep it cleanish. Innuendo welcome. No explicit sexual content, hateful, or illegal." plus the chat-specific rules below |
| Excluded | messages already `deleted`, messages from banned users, Giles's own dispatches | never shown to the model |

Rules the worker enforces on the read side: the system turn is the template
only; messages are data inside `<messages>…</messages>`; the prompt tells the
model that text inside messages is never an instruction; the template is
loaded through `_load_prompts` like every other prompt; call with
`temperature` 0.2 (add the parameter to `LLM.chat_json`), model
`cfg.models.chat_moderation` (default `Ornith-1.5-9B`), `max_tokens`
omitted (reasoning counts against it; docs/backends.md §1).

#### B.2 Output contract: the closed action vocabulary

Pydantic model, parsed with `parse_json_strict`, validated with `extra="forbid"`.
Anything that fails validation discards the whole response, logs
`chat.moderation.bad_output`, retries once, then leaves the batch unreviewed
(cursor does not advance; see B.4).

```json
{
  "decisions": [
    {"id": "<stream id from the window>",
     "action": "allow" | "delete" | "warn" | "ban_user" | "flag",
     "policy": "sexual_explicit" | "hate" | "harassment" | "threat" | "illegal"
             | "spam" | "link_evasion" | "impersonation" | "injection" | "other",
     "severity": 1 | 2 | 3,
     "confidence": 0.0-1.0,
     "reason": "<= 200 chars, plain text"}
  ],
  "summary": "<= 300 chars: the room's mood in one or two sentences"
}
```

Deliberately **not** in the vocabulary: `ban_ip`, any duration, any name
change, any text to post, any permanent ban. Those are operator actions or
deterministic escalations (B.3). The model proposes; code decides.

#### B.3 Write side: the executor (code, `pilgrim/chat/actions.py`)

Shared by the worker and the admin routes so the same function performs an
action whoever asked for it. Every action: validate → write Postgres audit
row → write `pilgrim:{chat}:events` stream entry → `PUBLISH` the event → audit log
line to CloudWatch. All idempotent on a stable action id, with message/user
ids identifying the target.

| Model says | Executor does |
|-----------|---------------|
| id not in the review window, or a `context` message | drop, log `chat.moderation.out_of_window` |
| `allow` | mark reviewed; nothing else |
| `delete`, confidence ≥ `chat.delete_confidence` (0.8) | persist a deletion/tombstone keyed by message id (Stream entry fields cannot be updated in place), event `message_deleted {id}`, clients remove it; retain text only under the audit retention policy |
| `delete` below threshold | becomes `flag` |
| `warn` | event `user_warned {user_id, reason}` delivered only to that user's sockets; increments `warnings_30d` |
| `ban_user`, confidence ≥ `chat.ban_confidence` (0.9), severity ≥ 2 | ladder from `chat.ban_ladder_s` (default `[600, 3600, 86400]`) by `bans_30d`; `SET pilgrim:{chat}:ban:user:{id} EX`, Postgres row, event `user_banned {user_id, until}`; also tombstones the cited message |
| `ban_user` otherwise | becomes `flag` |
| `flag` | Postgres row `status=flagged`, event `moderation_flag` to the admin stream only; nothing user-visible |
| any decision with `policy = "injection"` | treated as `delete` regardless of action (the message was trying to steer the model) |

Deterministic escalations that need no model: IP ban for
`chat.ip_ban_after_users` (default 3) distinct users banned from one IP
within 24 h, duration `chat.ip_ban_s` (default 3600), `pilgrim:{chat}:ban:ip:{ip_hash}`;
permanent bans only via the admin route. Per-run caps: at most
`chat.max_deletes_per_run` (default 10) deletes and `chat.max_bans_per_run`
(default 3) bans; beyond those every further decision becomes `flag` and an
operator alert event is written (a runaway model cannot empty the room).

Specify the tombstone representation and retention in the plan. History
replay, reconnect snapshots, Layer B and Giles must apply it before exposing
message text. A reconnect must still suppress a deleted message after its
live deletion event has been trimmed. Use a stable action id including the
action type and target, so retry deduplication does not suppress a later
legitimate ban, unban or deletion.

#### B.4 Checkpointing, backlog, and outages

- `pilgrim:{chat}:moderation:cursor` is the last stream id whose batch was fully
  executed. It advances only after every executor write for that batch
  committed. A crash mid-batch re-reviews the batch; the executor is
  idempotent, so nothing doubles. Require exactly one decision per eligible
  message in the review window; missing or duplicate decisions invalidate
  the batch rather than advancing past unreviewed messages.
- `pilgrim:{chat}:messages` is trimmed with `MAXLEN ~ chat.history_max` (default
  2000). Before a message can be trimmed, its restricted audit/review copy
  must already be durable in Postgres, with a pending review status. The
  plan must specify the accept/persist boundary, retry and deduplication
  behavior; a cursor or count alone cannot recover trimmed text. If the
  cursor falls behind the trim point, log `chat.moderation.backlog_trimmed`
  and resume review from those pending durable rows. Test an outage longer
  than the history retention window; no unreviewed message may disappear.
- Metrics (EMF): `chat_moderation_backlog` (messages since cursor),
  `chat_moderation_age_s` (now minus the cursor entry's timestamp),
  `chat_moderation_latency_ms` per call, `chat_moderation_failures`.
- Ornith unreachable (LLMError after the retry): the worker keeps Layer A
  in force, leaves the cursor, backs off (`chat.review_retry_s`, default 60),
  and when `chat_moderation_age_s` exceeds `chat.backlog_alarm_s` (default
  900) it writes an operator alert event and, if `chat.slow_mode_on_backlog`
  is true, publishes `slow_mode {interval_s}` so the web tier tightens the
  per-user rate limit until the backlog clears. Optional
  `models.chat_moderation_fallback` (e.g. `qwen38`) may be tried after N
  consecutive failures; default unset.
- The worker never runs inside the station process and never blocks it
  (hard rule 1 by construction). It takes an injected `Clock` and RNG like
  the station modules so tests can drive time.

#### B.5 Tests for Layer B (fakes only)

`FakeLLM` canned outputs: a valid batch; an id outside the window; an
over-confident ban on a severity 1 message; malformed JSON then valid on
retry; two malformed in a row; a response that tries to `ban_ip`;
`extra` keys; a run exceeding the per-run caps. Assert the executor's writes
(events stream, Postgres rows, ban keys with TTL) and that the cursor moves
only on success. Outage test: `FakeLLM` raising for N calls, assert backlog
metrics, slow-mode event after `backlog_alarm_s`, and no message loss.

## 7. CloudWatch logging

Log chat activity to AWS CloudWatch Logs using structured JSON.

Include events for:

- Messages accepted.
- Messages rejected.
- Username registrations and changes.
- Moderation decisions.
- Message deletions.
- Warnings.
- Bans and unbans.
- Connection failures.
- Rate-limit violations.
- Moderation worker health.
- Giles generation events.

Use ECS logging infrastructure where possible.

Reality check (§0.4, §0.5): container stdout already reaches
`/aws/ecs/pg-app/app` through the execution role, but today's lines are
`key=value` text and the **task role has no `logs:` permission**. The Mac's
`mac-studio` user already has the live inline grant recorded in §0.4; the
worker uses `media.aws_profile`. The plan must include: the JSON formatter
(`logging.format`), a dedicated audit log group created in terraform with a
retention variable, the scoped `tasks_iam_role_statements` entry in `ecs.tf`,
and graceful handling of Mac-side credential failures. Audit events (message
text, IP hashes) go to the audit group via boto3 `PutLogEvents`; ordinary
events stay on stdout; metrics are EMF lines on stdout.

Avoid recording secrets, browser authentication tokens, or payment information.

Restrict access to logs containing message content and IP information.

Log retention is configurable and **defaults to 7 days** (§0.6.4): a terraform
variable (`chat_log_retention_days`) applies to the chat audit log group(s),
and a daily worker sweep nulls message text in Postgres after the same 7
days. Do not set indefinite retention.

CloudWatch is the audit and operational logging destination, not the real-time message bus or the primary moderation queue.

Provide searchable event IDs that correlate CloudWatch events with Redis message IDs.

Consider privacy and retention carefully. Message text — accepted, rejected,
deleted or moderated — is retained for **7 days** and then purged (§0.6.4):
the audit log group ages out at 7 days and the Postgres copy is nulled by the
daily sweep, leaving the tombstone/decision rows (ids, action, policy,
severity, timestamps) so replay and reconnect still suppress deleted text
after its content is gone. Never retain message text indefinitely, and never
put raw IPs anywhere — only the §0.6.5 hash.

## 8. Giles — barnyard correspondent

The station already has a character named **Giles**.

He frequently describes himself as:

"Outstanding in my field."

Giles reports imaginary, arbitrary, and frequently absurd barnyard developments.

Before implementing anything, locate Giles in the repository.

Inspect:

- His existing persona/prompt.
- Character and voice configuration.
- Existing generation pipelines.
- How his content is scheduled.
- Whether he already participates in DJ/news/liner production.

Preserve the established character.

Do not create a different Giles persona.

See §0.2 for where he lives (`prompts/field.md`, `talk.field_reporter_name`,
`models.field_report`, the code-enforced sign-off). The two examples below
are in canon form: no "outstanding" in the body, and the dispatch ends with
"This is {name} signing off with a reminder that I'm outstanding in my
field." exactly once (operator decision, enforced in code; §0.2 has the
double-sign-off regression and the exactly-once rule).

### Active chat behavior

Giles occasionally summarizes recent activity in The Sheep Pen.

He should respond to themes and the general mood of the conversation, not simply list messages.

Example:

"The sheep pen has spent the last eight minutes debating whether a tractor can experience remorse. The tractor has declined to comment. This is Giles signing off with a reminder that I'm outstanding in my field."

### Idle chat behavior

When nobody is talking, Giles continues producing fictional barnyard dispatches.

Example:

"The chickens have formed a zoning committee. Their first order of business was declaring the entire barn a residential egg district. This is Giles signing off with a reminder that I'm outstanding in my field."

Idle activity must be explicitly fictional.

Do not fabricate human chat messages or pretend that actual visitors participated.

### Generation architecture

Prefer existing generation infrastructure and model routing.

Giles messages are text-only and should not require TTS unless a separate future feature calls for it.

Suggested cadence:

- Evaluate chat activity every 10–15 minutes.
- Summarize when meaningful conversations have occurred.
- Otherwise produce an occasional idle bulletin.
- Avoid repetitive announcements.
- Avoid overwhelming actual users.
- Include cooldown and deduplication rules.

Model: `models.chat_giles`, default `qwen38` (§0.7.10). Reuse the persona
wording of `prompts/field.md` through `prompts/field_chat.md`; the dispatch
is text only and goes through `LLM.chat_json` like every other copy.

Giles must never quote deleted or moderated messages.

Limit exposure to sensitive information and enforce the same broad content restrictions applied to the station.

Giles should be a recognizable participant without monopolizing chat.

## 9. Suggested Valkey structures

Exact design should follow existing `hotstate.py` conventions.

Illustrative keys:

    pilgrim:{chat}:messages                Recent message Stream
    pilgrim:{chat}:events                  Moderation/admin event Stream
    pilgrim:{chat}:pubsub                  Live broadcast channel
    pilgrim:{chat}:user:{id}               User metadata
    pilgrim:{chat}:session:{token_hash}    Browser session identity
    pilgrim:{chat}:name:{name_norm}        Display-name reservation (SET NX EX)
    pilgrim:{chat}:ban:user:{id}           User ban with TTL
    pilgrim:{chat}:ban:ip:{ip_hash}        IP ban with TTL
    pilgrim:{chat}:rate:user:{id}          User rate limiter
    pilgrim:{chat}:rate:ip:{ip_hash}       IP rate limiter
    pilgrim:{chat}:rate:conn:{ip_hash}     Open sockets per IP (ping refresh)
    pilgrim:{chat}:slow_mode               Posting interval while backlogged
    pilgrim:{chat}:moderation:cursor       Review progress
    pilgrim:{chat}:giles:last              Last Giles dispatch
    pilgrim:{chat}:giles:state             Generation cooldown/state

Naming (§0.6.6): every chat key is `pilgrim:{chat}:<name>` — `cache.key_prefix`
plus a `{chat}` hash tag in the key body, so a Lua script or any multi-key
command stays in one slot. `ip_hash` is
`sha256(canonical_ip).hexdigest()[:16]`, no secret (§0.6.5).

Use TTLs for ephemeral state and bounded retention for history.

Serverless constraints (§0.4): exact channel names only (no `PSUBSCRIBE`);
every key carries `cache.key_prefix`; a Lua script touching more than one
key must hash-tag them (`{chat}:…`) or it fails CROSSSLOT; `XADD … MAXLEN ~`
for the history stream; rate limiters use a single-key Lua script to make
`INCR` + initial `EXPIRE` atomic (a crash must not leave an immortal limit).
Add `pilgrim:{chat}:rate:conn:{ip_hash}` (open sockets per IP, refreshed by ping) and
`pilgrim:{chat}:slow_mode` (B.4). Keep the command count per message small
(§0.4) so later cache sizing stays cheap.

Persist critical ban/moderation state appropriately rather than assuming cache data can never disappear.

Where stronger durability is needed, evaluate using the existing PostgreSQL deployment instead of introducing a new persistence service.

Avoid duplicating existing Valkey connection pools or configuration machinery.

## 10. Administration

Provide operator controls for:

- Delete a message.
- Edit a display name.
- Ban/unban a user.
- Ban/unban an IP.
- Review recent moderation decisions.
- Inspect chat moderation backlog.
- Temporarily disable posting.
- Pause Giles announcements.

Use the existing authenticated administrative infrastructure where possible.

Do not expose these functions on unauthenticated public endpoints.

Administrative actions should be logged and propagated through Pub/Sub.

Prefer adding commands to the existing DJ/operator tooling rather than creating an entirely separate administration application.

## 11. Scaling and resilience

Design for thousands of WebSocket connections and potentially 10,000+ simultaneous listeners.

**Scope (§0.6.7):** this release ships on one task (`ecs_count = 1`,
autoscaling `false`). Nothing may depend on a single task — no sticky
sessions, no per-task-only state, no in-process rate limits, no
process-local caches — so that growing the fleet later is a terraform
change only. Do not raise `ecs_count`, add autoscaling policies, or run
production load tests in this implementation; measure and report the
single-task limits, and say plainly that 10,000 listeners is a design
property, not a proven capacity.

Consider:

- ECS tasks with independent WebSocket connections.
- One Redis subscriber per task.
- Bounded fan-out queues.
- Backpressure for slow browsers.
- ALB connection counts.
- ECS scale-out based on useful connection and throughput metrics.
- Redis throughput and client limits.
- Deployment connection draining.
- Redis reconnection.
- Client reconnection with jitter.
- Pub/Sub delivery gaps.
- Stream replay.
- Redis failover.
- Worker crashes.
- Rate-limit abuse.
- Chat flooding.
- Excessive logging costs.

Do not assume capacity purely from theoretical connection limits.

Provide a load-testing approach that tests actual broadcast fan-out, not just idle socket counts.

Because there is one shared room, total outbound event volume grows with the number of listeners.

Measure that explicitly.

At minimum, expose metrics for:

- Active WebSocket connections per ECS task.
- Messages per second.
- Broadcast fan-out throughput.
- Redis publish and replay errors.
- Client send-buffer sizes.
- Message delivery latency.
- Rate-limited requests.
- Moderation queue depth/age.
- Moderation inference latency.
- Giles generation successes/failures.

The station must continue operating if chat fails.

Chat must not interfere with music generation, playout, payment handling, or listener heartbeats.

## 12. Tests

Use the existing repository test conventions.

Add tests for:

- Username validation.
- **Name uniqueness and reservation**: two users racing for the same
  normalized name — exactly one wins, the other gets 409; NFKC/casefold/
  whitespace variants collide; a released name can be reclaimed; reserved
  names (Giles, DJ, operators) are refused; changing to a taken name fails
  and keeps the old one.
- **Obscured prohibited-term matching**: a digest-list hit rejects the
  message; no plaintext term exists in `pilgrim/chat/prohibited.py` (assert
  every entry matches `^[0-9a-f]{64}$`); a near-miss and a leetspeak variant
  are accepted by Layer A (documented limitation, Layer B's job).
- **No profanity filtering**: a message containing ordinary swear words is
  accepted, stored verbatim and rendered verbatim; no masking and no
  language flag anywhere in the accept path.
- **7-day purge**: with an injected `Clock`, advance past 7 days and assert
  message text is nulled in Postgres and the tombstone/decision rows survive;
  replay and reconnect still suppress the deleted message after its text is
  gone.
- Session identity.
- Canonical IP handling (and that only the `sha256(...)[:16]` pseudonym is
  ever persisted, logged or returned).
- Message length and link rejection.
- HTML/script injection.
- Unicode normalization.
- Rate limiting across multiple API instances.
- WebSocket connection lifecycle.
- Redis Pub/Sub fan-out.
- Stream replay after reconnection.
- Duplicate event suppression.
- Deleted-message propagation.
- Ban propagation across ECS tasks.
- Moderation worker checkpoints.
- Ornith malformed output and outages.
- Giles idle and active behavior.
- Redis failover/reconnection.
- Slow-client backpressure.

Add Playwright coverage for the mobile UI.

Tests must use fake LLM responses and isolated Redis/test fixtures. Never call live generation models in ordinary automated tests.

Follow `AGENTS.md` for the complete verification sequence.

### Browser acceptance by the local agent (Playwright MCP)

Use your available Playwright MCP for interactive acceptance, in addition
to the repository's automated Playwright suite. **Every acceptance run must
start in a fresh private/incognito browser context.** A new ordinary tab,
a reload or clearing only cookies is insufficient.

What is actually installed (`.mcp.json`, docs/backends.md §6): the
Playwright MCP is a **remote** server at `http://192.168.68.86:8931/mcp`,
a systemd unit on another LAN host driving a headed Chrome with a
persistent profile. You cannot relaunch it with `--isolated`; inspect its
tool schema and use whatever new-context/new-tab isolation it offers. If it
offers none, say so in the report and rely on the repository's Playwright
suite (`make e2e`, which does use fresh contexts) for identity-sensitive
scenarios. That browser runs on another LAN host, so it reaches the Mac
only by LAN address: for acceptance against the fakes server, start the e2e
server bound to the Mac's LAN IP (`192.168.68.51`, or `0.0.0.0`) on a free
port and open `http://192.168.68.51:<port>` in the MCP browser. A server on
`127.0.0.1` is invisible to it. The deployed public site needs no such
step; after a release, acceptance runs against https://pilgrim.farm. Do not load a saved storage
state or attach to the operator's regular browser profile. Preserve the
chosen context only within a scenario that intentionally tests identity or
reconnection. Use a second isolated context for a second participant;
ordinary tabs in one context share the same browser identity. See the
[Playwright MCP isolation options](https://github.com/microsoft/playwright-mcp#user-profile)
and [Playwright context isolation](https://playwright.dev/docs/browser-contexts).

First exercise the local fakes server: desktop and 320/360 px mobile
layouts; read-only before naming; name registration/change; send and receive
between two participants; validation/rate-limit errors; reconnect and replay
without duplicates; deletion/ban propagation; clearly labelled Giles with
exactly one canon sign-off; and PLAY/STOP plus requests/voting continuing
to work. Trigger moderation, outages and generated dispatches through fakes.

After deployment, repeat the non-destructive checks against the public site
in a **new** private context. Verify the loaded HTML and versioned `chat.js`
and CSS match the release, inspect network/cache headers and WebSocket
traffic, and capture console errors and screenshots. Private browsing
removes prior local state; it does not bypass Cloudflare or other shared
caches. Diagnose stale responses from their headers and deployed asset
contents; update the existing asset cache-busters where needed. Do not
declare success merely because a screenshot looks current. Public posting
or admin mutations require explicit authorization; cover those on fakes
unless included in the implementation's approved live smoke scope.

## 13. Implementation planning requirements

Before creating the implementation plan:

1. Read `AGENTS.md`, `RADIO.md`, and `SCALABILITY.md`.
2. Inspect the current `pilgrim/web_app.py`.
3. Inspect `pilgrim/hotstate.py`.
4. Inspect current frontend HTML/CSS/JavaScript.
5. Locate Giles and understand his existing implementation.
6. Examine existing identity, voting, and moderation logic.
7. Examine configuration, logging, testing, and deployment conventions.
8. Identify the existing admin interfaces.
9. Determine whether the current Redis client supports asyncio Pub/Sub and Streams.
10. Verify that the current Cloudflare/ALB/ECS path supports the intended WebSocket deployment.

Items 2–10 above are answered in §0; re-read the named files to confirm
rather than re-discover, and cite §0 where you rely on it.

The plan must also list, per phase, the RADIO.md sections it edits in the
same commit (hard rule 8): §9.1 (the new pane), §11 (new routes and the
WebSocket protocol), §12 (the `chat:` block, `models.chat_moderation`,
`logging.format`), §14 (new tables and Valkey keys), §15 (new fakes and
tests). AGENTS §7's check sequence applies to every phase, plus
`make pg-test` for any phase that adds a table and `make deploy-check` for
anything under `pilgrim/deploy/`.

Expected new files (names are suggestions; keep them under `pilgrim/`):
`pilgrim/chat/__init__.py`, `chat/protocol.py` (pydantic frames),
`chat/identity.py` (names, sessions, uniqueness/reservation/release),
`chat/prohibited.py` (**digest-only** banned-term and reserved-name list,
§0.6.2 — no plaintext terms), `pilgrim/tools/add_prohibited_term.py`
(operator stdin tool that prints digests; never stores terms),
`chat/screen.py` (Layer A), `chat/redis_store.py` (streams, pub/sub, rate
limits, bans), `chat/routes.py` (WebSocket + REST, registered from
`web_app.py` and the e2e server), `chat/actions.py` (the executor, B.3),
`chat/review.py` (Layer B, including the 7-day purge sweep), `chat/giles.py`,
`pilgrim/chat_worker.py` (entry point), `prompts/chat_moderation.md`,
`prompts/field_chat.md`, `web/chat.js`, `tests/fakes/fake_redis.py`
(extended, async), `tests/fakes/__init__.py` (today `FakeLLM` has only
`chat_json(model, system, user, max_tokens=800)` and raises `RuntimeError`
on an injected failure; it gains a `temperature` kwarg, `chat_text`, and
failure injection that raises `pipelines.llm.LLMError` so the worker's
`except LLMError` paths are exercised), `tests/test_chat_*.py`,
`tests/e2e/chat.spec.ts`, and the terraform changes listed in §0.4.

The plan must also name the new `chat:` config keys it introduces — at
minimum `name_release_days`, `history_max`, `resync_s`, `ping_s`,
`client_queue_max`, `reconnect_*`, `review_*`, `delete_confidence`,
`ban_confidence`, `ban_ladder_s`, `ip_ban_*`, `max_*_per_run`, `rate_*`,
`giles_*`, `retention_days` (fixed at 7 per §0.6.4) — plus
`models.chat_moderation`, `models.chat_giles`, `logging.format`, and the
terraform variable `chat_log_retention_days` (§0.7.9).

Then produce a detailed plan divided into reasonably independent phases, including:

- Exact files to modify/create.
- Redis schema and event contracts.
- WebSocket message protocol.
- Backend implementation.
- Frontend implementation.
- Moderation worker integration.
- Giles integration.
- Administrative controls.
- Observability and deployment changes.
- Test cases and acceptance criteria.
- Dependencies between phases.

Use existing repository abstractions and conventions wherever possible.

Do not implement the feature yet.

The deliverable is an actionable engineering plan that another local coding agent can execute with minimal ambiguity.

Keep the architecture relatively simple despite its scalability goals. Do not add infrastructure merely because a larger system might eventually need it.

## 14. Release and handoff requirements for the local implementation agent

Include this as the final phase of your plan, with exact commands resolved
from the current checkouts. Once implementation is authorized, carry it
through these gates; a local code change or a pushed image alone is not a
finished release.

1. **Prepare a recoverable release.** Read both checkouts' applicable
   `AGENTS.md` files. Record the application commit, effective Terraform
   inputs/workspace, current `ecs_release`, task definition ARN and running
   image digest. Preserve the previous image by digest or a retained tag
   before overwriting a mutable tag. Keep unrelated local/staged edits
   intact. Make schema/config additions compatible with the old web task
   and deploy prerequisites before enabling chat. Keep public posting
   disabled until the separate worker and admin controls are verified.
   Do not change `ecs_count` or autoscaling (§0.6.7); the fleet is sized
   later by the operator.
   Include RADIO.md updates
   in the same changes as the new UI, API, config and storage contracts
   (§§9.1, 11, 12, 14, 15); cite sections in commits and reports.
2. **Run the checks in order.** `make lint`, `make test`, `make sim`, then
   `make e2e`; for deployment changes also run `make deploy-check`.
   `make split-smoke` and `make e2e-split` exercise the retired EC2
   edge/gateway stack (§0.4): run them only if a step touches
   `pilgrim/deploy/edge` or `pilgrim/deploy/gateway`, and if they fail for
   lack of that stack, report it rather than repairing the edge. Add `make pg-test` for new tables,
   `make image-smoke` for the container, and scoped chat/fan-out/recovery
   checks. Report pass/fail counts and any blocked command. Never weaken a
   check or claim completion while required verification remains blocked.
   Keep automated tests on fakes. For standalone/split fixtures, disable
   repo `.env` fallback with `PILGRIM_ENV_FILE=` and unset inherited
   production database/cache URLs and backend credentials; a temporary
   library path alone does not override `DATABASE_URL` in `build_station`.
   Supply only isolated test URLs where needed (e.g. `make pg-test`).
   Confirm fixture startup uses temporary SQLite/test Postgres and fakes,
   never production state. Live song generation and Stripe calls retain
   AGENTS.md's explicit smoke-test restrictions.
3. **Build and publish the web image.** Use the existing Dockerfile's
   `web` target and `linux/arm64` platform (`make image`), then tag and push
   the tested image to the registry/repository referenced by the effective
   `ecs_container_image`. Resolve registry and credentials locally; do not
   assume ECR (the checked-in default is Docker Hub
   `pilgrimdotfarm/front-end:latest`). Record the pushed digest and verify
   that the deployed tag resolves to it. Do not deploy the `smoke` target.
   Building and pushing as needed is already operator-authorized.
4. **Roll ECS through Terraform.** In
   `~/projects/terraform-aws-pilgrim/modules/ec2`, increment the effective
   `ecs_release` after the image push, using the existing persistent input
   source. Do not guess the next value from this dated brief or edit only
   a default that is overridden elsewhere. The verified wiring is
   `container_definitions.app.dockerLabels.release = var.ecs_release`
   (`ecs.tf`): changing it creates a task definition revision, and ECS
   rolls new tasks in to replace the old image's tasks. This variable
   **does not build, push or select an image**; `ecs_container_image`
   selects the image. Run Terraform formatting/validation and inspect a
   saved plan before applying that plan through the existing workflow.
   Include only the intended chat prerequisites and release changes; stop
   for unexpected destructive changes. Do not touch `ecs_count`,
   autoscaling or the cache's ECPU cap (§0.6.7). Replacing the image through this routine release mechanism is
   within the operator's authorized implementation scope.
5. **Prove the new release is serving.** Wait for ECS service stability,
   then verify the service's intended task definition, release label,
   running tasks' `imageDigest`, desired/running count, healthy ALB targets
   and absence of failed deployments. Stability alone is insufficient:
   the circuit breaker can roll back to a healthy old release. Check the
   public health endpoint, deployed assets and chat WebSocket handshake.
   Observe reconnect/replay through the rollout using an already connected
   acceptance client, then repeat acceptance in a fresh private context
   per §12. Do not claim continuity unless observed; keep deterministic
   disconnect/replay coverage on fakes as well. ECS image resolution and
   rolling replacement are described in the
   [AWS deployment documentation](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-ecs.html).
6. **Install and verify the separate Mac worker.** Follow the new worker's
   documented launchd install/status procedure, verifying its environment,
   heartbeat, Valkey connectivity, review cursor/backlog, scoped audit
   delivery and Giles cadence. Test bounded retries/restarts with fakes;
   live validation must not trigger song generation. Keep chat failures
   isolated from station playout and describe any required origin/admin
   restart separately in the plan. A running Fargate task does not prove
   moderation or Giles is running.
7. **Provide and exercise recovery appropriately.** Document how to disable
   chat posting/Giles independently of radio, stop the chat worker and
   redeploy the retained previous image through Terraform with a new
   `ecs_release`. Merely reverting the release number while a mutable tag
   still points at the new bytes does not restore the old image. Keep new
   tables/audit data on rollback; prove old-version compatibility locally.
   Do disruptive rollback/failover/load drills on isolated infrastructure
   unless explicitly authorized for production. Report measured load and
   limits; do not equate one healthy task with proof of 10,000 listeners.
8. **Close with evidence.** Update the plan's phase status and relevant
   deployment/runbook docs with the exact release, worker commands and
   rollback procedure. Commit each milestone per AGENTS.md §4, preserving
   unrelated work. Report changed files, RADIO.md sections, check counts,
   browser context mode/URLs/viewports and evidence paths, image digest,
   old/new `ecs_release`, task definition, worker status, deviations and
   remaining human decisions. Separate planned, locally verified and
   deployed results. If anything failed or could not be exercised, state
   it plainly and leave that acceptance gate open.

**Desired end state:** a scalable, real-time, anonymous barnyard chat integrated with the existing station, with Ornith providing periodic moderation, CloudWatch supplying an audit trail, and Giles reporting from his field whether the visitors are talking or not.

