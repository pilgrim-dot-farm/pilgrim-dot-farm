# SHEEP_PEN_PLAN.md — The Sheep Pen chat (implementation plan)

> **Read this first.** This is the executable plan derived from `SHEEP_PEN.md`
> (the brief). `RADIO.md` and `AGENTS.md` outrank it; within the brief the
> order is §0.6, §0.7, §0.5, then the rest. Every step ends with a *Done when*
> line, the AGENTS.md §7 checks, a commit citing RADIO.md sections, and the §8
> report. Choices the brief left open are recorded under **Decisions**;
> dated facts that drifted are recorded under **Drift log**. Cite this plan as
> `SHEEP_PEN_PLAN §n`.
>
> Scope note (§0.6.7): one Fargate task, `ecs_count = 1`, autoscaling off, the
> Valkey ECPU cap at 1000. Nothing in this release depends on a single task, so
> growing the fleet later is a terraform-only change. No capacity or
> autoscaling work is in scope. 10,000 listeners is a design property, not a
> proven capacity.

## Objective and shape

A public, real-time, anonymous chat — **THE SHEEP PEN** — on
https://pilgrim.farm, between the request line and the hit counter. One
shared room. WebSockets + Valkey Pub/Sub for live delivery, Valkey Streams for
history and replay, Postgres for the durable audit copy, Ornith for periodic
moderation, and Giles (the existing field reporter) filing text dispatches into
the room when it is busy or idle.

Places the work lands (§0.5):

- **Fargate web tier** (`pilgrim/web_app.py` + a new `pilgrim/chat/` package):
  sockets, Layer A, rate limits, history replay, one Pub/Sub subscriber per
  task, the admin *events* producer.
- **Mac worker** (`python -m pilgrim.chat_worker`, its own launchd unit): Layer
  B (Ornith review), Giles generation, the 7-day purge sweep, the audit log
  writer for worker actions.
- **Mac station process** (`server.py` admin listener only):
  `/api/admin/chat/*` behind `require_dj`; it writes to Valkey, which every
  web task observes. No chat code on the public listener of the station.
- **Terraform checkout** (`~/projects/terraform-aws-pilgrim/modules/ec2`):
  audit log group, retention variable, scoped task-role `logs:` statement,
  `ecs_release` bump. Not in this repo; listed in Step 11.

## Decisions (the brief said "the plan must specify")

1. **Identity.** Browser token = the existing `pd_voter` 32-hex id
   (`window.PD_VOTER.id()`), validated server-side against
   `^[0-9a-f]{32}$`. `token_hash = sha256(token).hexdigest()` (64 hex) is the
   only server-side handle. `pilgrim:{chat}:session:{token_hash}` maps to a
   server-generated `user_id` (16 hex, `uuid4`). Postgres `chat_users` is the
   authority; the session key is a cache. A first connection creates the row
   (no name yet); a name claim fills it.
2. **Name reservation.** Normalized key = NFKC → casefold → collapse
   whitespace → strip. Display length 3–24 *code points* after strip. Reject
   control (`Cc`), format/invisible (`Cf`), `<`/`>`, URLs (the existing
   `request_filter.prefilter`), prohibited terms (digest list) and reserved
   names (digest list: Giles, Rosie Thistlebottom, operator/admin/moderator/
   system). `pilgrim:{chat}:name:{name_norm}` = `SET NX EX
   name_release_days*86400` is the cheap pre-check; Postgres
   `chat_users.name_norm` with a partial unique index (`WHERE released_at IS
   NULL`) is the authority inside the store lock. First claim wins; a loser
   gets 409 with "that name is taken" and no further detail. A change to a
   free name keeps `user_id`; the old reservation is deleted. Changes are
   capped at `chat.name_changes_per_day` (5) per 24 h via one Redis `INCR` with
   TTL. Release: worker daily sweep for `released_at IS NULL` users whose
   `last_seen` is older than `chat.name_release_days`, plus the admin route.
3. **IP pseudonym.** `ip_hash = sha256(canonical_ip).hexdigest()[:16]`, no
   secret (§0.6.5). Raw IPs never stored, logged, returned, or put in a frame.
   `PublicSurface._client_ip` is widened to accept
   `starlette.requests.HTTPConnection` and reused (no copy).
4. **Valkey keys.** All under `pilgrim:{chat}:` (§0.6.6): `messages`,
   `events`, `pubsub`, `session:{token_hash}`, `user:{id}` (hash),
   `name:{name_norm}`, `sent:{user_id}:{client_msg_id}`, `ban:user:{id}`,
   `ban:ip:{ip_hash}`, `rate:user:{id}`, `rate:ip:{ip_hash}`,
   `rate:conn:{ip_hash}`, `rate:namechg:{user_id}`, `deleted` (set),
   `moderation:cursor`, `worker:heartbeat`, `slow_mode`, `posting_disabled`,
   `giles:paused`, `giles:last`, `giles:state`. Values are JSON except the
   counters, the cursor, the deleted set and the stream fields.
5. **Message identity.** The `XADD` stream id **is** the message id
   everywhere (frames, Postgres `chat_messages.id`, audit, moderation rows).
   `client_msg_id` (UUID from the browser) makes a resend idempotent: one
   `SET NX EX 300` key holds the resulting id.
6. **Accept/persist boundary.** Order is exactly §0.7.3: Layer A → ban checks
   → rate limits (single-key Lua `INCR`+`EXPIRE`) → `XADD MAXLEN ~` →
   Postgres insert `status='pending_review'` (failure ⇒ `XDEL` + `retry`) →
   `PUBLISH` (failure logged; resync delivers) → audit line → `ack`. Because
   the Postgres row exists before a message can be trimmed out of the stream,
   the review worker can always recover trimmed-but-unreviewed text from
   `chat_messages WHERE status='pending_review'`.
7. **Delivery and gaps.** One subscriber per task on the exact channel
   `pilgrim:{chat}:pubsub` (`PSUBSCRIBE` is unavailable on serverless). Frames
   go into a bounded `asyncio.Queue(maxsize=chat.client_queue_max)` per socket;
   a full queue closes that socket with 1013. The subscriber tracks the last
   stream id it delivered and every `chat.resync_s` runs `XRANGE (last_id +`
   so publish failures, Pub/Sub gaps and cache failover are all covered by one
   loop. On error it reconnects with backoff and resubscribes.
8. **Tombstones.** Deletion writes Postgres (`deleted_at`, `deleted_by`),
   `SADD pilgrim:{chat}:deleted <id>`, an `events` stream entry and a live
   `deleted` frame. Every text-exposing path (history, welcome, review window,
   Giles context) filters through `SMISMEMBER` and falls back to Postgres when
   the set key is missing. The set is pruned daily to the ids still present in
   the stream.
9. **Frames.** §0.7.6 exactly. Client → `hello`, `name`, `send`, `ping`.
   Server → `welcome`, `message`, `ack`, `deleted`, `renamed`, `warned`,
   `banned`, `slow_mode`, `error`, `pong`. No browser frame carries a user id,
   session/token hash, IP or IP hash. Internal Pub/Sub envelopes may carry a
   target `user_id` for warn/ban targeting; each task filters before fan-out.
10. **Moderation executor.** One `chat/actions.py` shared by the worker and
    the admin routes. Actions are idempotent on
    `action_id = f"{action}:{target}:{until-or-window}"` stored as the unique
    `chat_moderation.action_id`. Caps: `max_deletes_per_run` (10) and
    `max_bans_per_run` (3) per worker run; beyond that the decision becomes
    `flag` and an operator alert event is written.
11. **Retention.** `chat.retention_days = 7` and terraform
    `chat_log_retention_days = 7`, kept equal and stated so in RADIO §12. A
    daily worker sweep nulls `chat_messages.text`, `chat_moderation.text` and
    the audit copy while keeping tombstone/decision rows.
12. **Audit log.** Ordinary events stay on stdout (JSON formatter selected by
    `logging.format`; container overlay sets `json`) and reach
    `/aws/ecs/pg-app/app`. Message text and IP hashes go **only** to the
    dedicated group `/aws/pilgrim/chat-audit` through boto3 `PutLogEvents`,
    built from `media.aws_profile` on the Mac and from the task role on
    Fargate. Metrics are EMF JSON lines on stdout. A missing credential is a
    logged degradation, never a crash.
13. **Giles.** Separate worker loop in `chat_worker.py`, `models.chat_giles`
    (`qwen38`), text only, never TTS, never the producer/scheduler. Cadence
    `chat.giles_interval_s` (600) with `chat.giles_cooldown_s` (900) between
    dispatches. Active summary when ≥ `chat.giles_min_messages` (6) non-deleted
    human messages since the last dispatch; otherwise an idle fictional
    bulletin at `chat.giles_idle_interval_s` (1800). Exactly-once canon
    sign-off enforced in code.
14. **Admin.** `/api/admin/chat/*` on the Mac admin listener behind
    `require_dj`: delete message, ban/unban user, ban/unban IP, release/rename
    a name, disable posting, pause Giles, read state (backlog, cursor, recent
    decisions). TUI bindings are listed as an open question below because the
    Textual console has no chat screen today; the HTTP surface is complete and
    testable without one.
15. **Config keys.** New `chat:` block: `enabled`, `max_length`,
    `name_min`, `name_max`, `name_release_days`, `name_changes_per_day`,
    `history_max`, `resync_s`, `ping_s`, `client_queue_max`,
    `reconnect_min_s`, `reconnect_max_s`, `review_interval_s`, `review_batch`,
    `review_context`, `review_retry_s`, `backlog_alarm_s`,
    `slow_mode_on_backlog`, `delete_confidence`, `ban_confidence`,
    `ban_ladder_s`, `ip_ban_after_users`, `ip_ban_s`, `max_deletes_per_run`,
    `max_bans_per_run`, `rate_user_s`, `rate_ip_per_min`, `rate_conn_per_ip`,
    `repeat_window_s`, `slow_mode_s`, `giles_interval_s`,
    `giles_idle_interval_s`, `giles_min_messages`, `giles_cooldown_s`,
    `giles_max_chars`, `retention_days`, `audit_log_group`. Plus
    `models.chat_moderation` (`Ornith-1.5-9B`), `models.chat_giles` (`qwen38`),
    `logging.format: text|json`. No new dependency: `websockets` comes with
    `uvicorn[standard]`, `redis.asyncio` with `redis==8.1.0`, boto3/psycopg are
    pinned.

## Steps

### Step 1 — Config, logging and the LLM temperature knob

Files: `pilgrim/config.py`, `pilgrim/config.yaml`,
`pilgrim/deploy/container/overlay.yaml`, `pilgrim/logging_setup.py`,
`pilgrim/pipelines/llm.py`, `pilgrim/tests/fakes/__init__.py`,
`pilgrim/tests/test_config.py`, `pilgrim/tests/test_logging.py`.

- Add `Chat` (Decisions 15) with validation: `max_length > 0`, `name_min <=
  name_max`, `ping_s <= 60` (Cloudflare's 100 s idle limit, §0.4),
  `resync_s > 0`, `retention_days == 7` (a `model_validator` warning/error
  keeps it equal to the terraform default), positive rates.
- `Config.chat`, `Models.chat_moderation`, `Models.chat_giles`,
  `Logging.format`.
- `logging_setup`: `JsonFormatter` (one JSON object per line: `ts`, `level`,
  `logger`, `msg`, extras, `exc`), selected by `logging.format`. Container
  overlay sets `json`.
- `LLM.chat_json`/`chat_text` gain `temperature: float = 0.8`; pass it in the
  payload. No other call site changes behaviour.
- `FakeLLM.chat_json` gains `temperature`; `FakeLLM.chat_text`; failure
  injection raises `pipelines.llm.LLMError` (not `RuntimeError`) so worker
  `except LLMError` paths are exercised. Keep `fail_after` semantics.

*Done when* `make lint` and `make test` pass; `load_config()` validates the new
block; a JSON-format logging test asserts one parseable object per record.

### Step 2 — Store tables and queries (RADIO §14)

Files: `pilgrim/store.py`, `pilgrim/tests/test_store_chat.py`, `RADIO.md §14`.

New tables in the one schema block (engine-neutral; no branching):

- `chat_users(user_id TEXT PK, token_hash TEXT UNIQUE, name TEXT, name_norm
  TEXT, ip_hash TEXT, created_at TEXT, last_seen TEXT, released_at TEXT,
  name_changed_at TEXT)` + partial unique index on `name_norm` where
  `released_at IS NULL AND name_norm IS NOT NULL`.
- `chat_messages(id TEXT PK, user_id TEXT, name TEXT, text TEXT, ts REAL,
  client_msg_id TEXT, status TEXT, flags TEXT, deleted_at TEXT,
  deleted_by TEXT)` + index on `ts`, unique `(user_id, client_msg_id)`.
- `chat_bans(id INTEGER PK AUTOINCREMENT, scope TEXT, target TEXT, ip_hash
  TEXT, until REAL, created_at TEXT, reason TEXT, active INTEGER)`.
- `chat_moderation(id INTEGER PK AUTOINCREMENT, action_id TEXT UNIQUE,
  message_id TEXT, user_id TEXT, action TEXT, policy TEXT, severity INTEGER,
  confidence REAL, reason TEXT, text TEXT, created_at TEXT, decided_by TEXT)`.

Methods: `create_chat_user`, `chat_user_by_token`, `chat_user`,
`claim_chat_name` (returns `(ok, user)`; raises `IntegrityError`-safe),
`release_chat_name`, `touch_chat_user`, `release_stale_chat_names`,
`insert_chat_message`, `chat_message`, `mark_chat_message_deleted`,
`chat_messages_after`, `recent_chat_messages`, `pending_chat_messages`,
`purge_chat_text_before`, `insert_chat_ban`, `active_chat_ban`,
`deactivate_chat_ban`, `insert_chat_moderation` (idempotent on `action_id`),
`recent_chat_moderation`, `chat_backlog`.

*Done when* `make test` and `make pg-test` pass (partial index and
`AUTOINCREMENT` both survive `db.py`'s rewriting); the new tables exist on both
engines.

### Step 3 — Chat package: identity, screening, protocol

Files: `pilgrim/chat/__init__.py`, `protocol.py`, `identity.py`,
`prohibited.py`, `screen.py`, `pilgrim/tools/add_prohibited_term.py`,
`prompts/chat_moderation.md`, `prompts/field_chat.md`,
`pilgrim/tests/test_chat_identity.py`, `test_chat_prohibited.py`,
`test_chat_screen.py`.

- `prohibited.py`: **digests only** — `PROHIBITED_DIGESTS: frozenset[str]`
  (SHA-256 hex of normalized tokens/phrases) and `RESERVED_NAME_DIGESTS`.
  `normalize(text)`, `tokens(text)`, `hits(text)` (token + bigram + trigram
  digests), `is_reserved(name)`. A module-level assertion keeps every entry
  `^[0-9a-f]{64}$`. `tools/add_prohibited_term.py` reads terms from stdin,
  prints digests, never stores or echoes them anywhere else.
- `identity.py`: token validation, `token_hash`, `resolve_session`,
  `validate_name` (returns a stable error code), `normalize_name`,
  `claim_name`, `release_name`, `change_name`.
- `screen.py`: Layer A — trim, nonempty, code-point length, control/format
  characters, `prefilter` (links/contact), prohibited digest hit, repeat
  detection, and flags (`repeat`, `near_limit_rate`, `name_recent`). No
  profanity list, no masking, no language flag (§0.6.3).
- `protocol.py`: pydantic models for every client and server frame with
  `extra="forbid"` on client frames; helpers `message_frame`, `welcome_frame`,
  `error_frame`. `MAX_TEXT` comes from config, never a literal.
- `prompts/chat_moderation.md` and `prompts/field_chat.md` are derived from
  the current `field.md`/`moderation.md` wording (no "pun in the body" line;
  the sign-off appears once).

*Done when* the three test files pass: name uniqueness/409/release/reserved,
digest format and hit, near-miss/leetspeak accepted (documented limitation),
swear words accepted verbatim, length/link/HTML/Unicode/control cases.

### Step 4 — Async Valkey client, streams, rate limits, pub/sub

Files: `pilgrim/chat/redis_store.py`, `pilgrim/tests/fakes/fake_redis.py`
(add `FakeAsyncRedis`), `pilgrim/tests/test_chat_redis.py`.

- `ChatStore` owns one `redis.asyncio.Redis.from_url(url,
  socket_timeout=cfg.cache.timeout_s, socket_connect_timeout=..., decode_responses=True)`,
  same URL/config as `hotstate.connect`, one `pubsub()` per task. `HotState`
  stays sync and untouched.
- Methods: `xadd_message`, `xrange_after`, `recent`, `publish`, `subscribe`,
  `rate_hit(key, ttl)` (Lua `INCR`+`EXPIRE`), `sadd/smismember/srem`, ban
  `get/set(ex)`, json `get/set`, `delete`, `incr`, `exists`, `xtrim`, `xlen`.
  Every multi-key command is hash-tagged `{chat}`.
- `FakeAsyncRedis`: the full slice above, TTLs against an injected clock,
  in-instance Pub/Sub (one queue per subscriber per channel), `eval`
  implementing the single `INCR`+`EXPIRE` script, `fail_next` injection,
  `CROSSSLOT` on cross-slot multi-key ops. Extend `FakeRedis` only where a
  shared helper is needed.

*Done when* `test_chat_redis.py` covers rate limiting (atomic TTL), stream
add/range/trim, Pub/Sub fan-out to two subscribers, replay after reconnect,
duplicate suppression, deleted-set membership and failover reconnect.

### Step 5 — Routes: WebSocket + REST, accept path, fan-out, shutdown

Files: `pilgrim/chat/routes.py`, `pilgrim/chat/hub.py`,
`pilgrim/web_app.py`, `pilgrim/tests/e2e/e2e_server.py`,
`pilgrim/tests/test_chat_api.py`, `RADIO.md §11`.

- `register_chat_routes(app, cfg, store, chat, hub)` mounted from
  `create_web_app` and the e2e server **before** `_add_api_404`. Routes:
  `WS /api/chat/ws`, `POST /api/chat/name`, `GET /api/chat/history?after=`.
  Both REST responses `Cache-Control: no-store`.
- Accept path exactly Decisions 6. `hello` → welcome (name, banned_until,
  slow_mode, last_id, history). `send` → Layer A/ban/rate/persist/publish/ack.
  `ping`/`pong` every `ping_s`, close after two misses. `name` frame delegates
  to the REST handler. Full queue ⇒ close 1013.
- `hub.py`: connection registry keyed by `user_id` (never exposed), per-socket
  bounded queue, `broadcast(frame, target_user=None)`, `close_all(1012)` on
  SIGTERM/shutdown, one subscriber task started in the app lifespan with
  reconnect/backoff and `XRANGE` resync.
- Rate-limit connection establishment per IP (`rate:conn:{ip_hash}`,
  `chat.rate_conn_per_ip`, refreshed by ping).

*Done when* `test_chat_api.py` drives TestClient `websocket_connect`: hello,
read-only before naming, claim/409, send/ack, duplicate `client_msg_id`,
rate-limit error, banned, slow mode, deleted propagation, replay, two
participants, shutdown 1012.

### Step 6 — Executor (Layer B write side) and admin routes

Files: `pilgrim/chat/actions.py`, `pilgrim/chat/audit.py`,
`pilgrim/server.py` (`_add_admin_routes`), `pilgrim/tests/test_chat_actions.py`,
`RADIO.md §11`.

- `actions.py` implements Decisions 10: validate → Postgres audit row → events
  stream → `PUBLISH` → audit log. Delete/tombstone, warn (targeted envelope),
  ban (ladder by `bans_30d`, TTL key + Postgres row + targeted event +
  tombstone of the cited message), flag (admin-only event), `injection`
  forced to delete, deterministic IP ban after `ip_ban_after_users` distinct
  banned users on one `ip_hash` within 24 h. Stable `action_id` so retries do
  not suppress a later legitimate action.
- `audit.py`: `AuditLog.write(event, **fields)` → boto3 `logs` client from
  `cfg.media.aws_profile`, `CreateLogStream` then `PutLogEvents`, sequence
  token handling, one warning on credential failure, never raises. Tests use a
  fake.
- Admin routes behind `require_dj` on the Mac admin listener only, lazily
  opening an async `ChatStore` from `REDIS_URL`; `503` when Valkey is
  unreachable. They call the same executor, so web tasks see the result via
  Pub/Sub. No admin routes on the web tier (`_add_api_404` still 404s them).

*Done when* `test_chat_actions.py` asserts Postgres rows, event-stream
entries, ban keys with TTL, per-run caps, idempotency and the IP-ban ladder;
admin route tests pass on the admin app and 404 on the web app.

### Step 7 — Layer B review worker, purge sweep, metrics

Files: `pilgrim/chat/review.py`, `pilgrim/tests/test_chat_review.py`.

- `Reviewer.run_once()`: read cursor, gather the review window
  (`XRANGE (cursor +`, ≤ `review_batch`) plus `review_context` tail, author
  dossiers, Layer A flags; system turn = `prompts/chat_moderation.md` only,
  messages JSON-escaped inside `<messages>`; `LLM.chat_json(
  models.chat_moderation, ..., temperature=0.2)` with `max_tokens` omitted.
- Pydantic `ChatReview` with `extra="forbid"`, closed action/policy
  vocabulary, severity 1–3, confidence 0–1, reason ≤ 200, summary ≤ 300.
  Exactly one decision per eligible message; missing/duplicate/out-of-window
  invalidates the batch (no cursor advance). Malformed → log
  `chat.moderation.bad_output`, retry once, then leave the batch.
- Cursor advances only after every executor write commits. A batch older than
  the stream trim is recovered from `chat_messages status='pending_review'`
  (`chat.moderation.backlog_trimmed`).
- Outage: keep Layer A, leave cursor, back off `review_retry_s`; when
  `chat_moderation_age_s > backlog_alarm_s` write an operator alert and (if
  `slow_mode_on_backlog`) set `pilgrim:{chat}:slow_mode` + publish
  `slow_mode`. Optional `models.chat_moderation_fallback` after N failures.
- `purge()`: null text older than `retention_days` in both tables; prune the
  deleted set to ids still in the stream; release stale names.
- EMF metrics: `chat_moderation_backlog`, `chat_moderation_age_s`,
  `chat_moderation_latency_ms`, `chat_moderation_failures`,
  `chat_messages_per_s`, `chat_fanout_frames`, `chat_delivery_latency_ms`,
  `chat_ws_connections`, `chat_rate_limited`, `chat_publish_errors`,
  `chat_replay_errors`, `chat_giles_success`, `chat_giles_failure`.

*Done when* `test_chat_review.py` covers the brief's §B.5 list: valid batch,
out-of-window id, over-confident ban on severity 1, malformed→valid retry,
two malformed, `ban_ip` attempt, `extra` keys, caps exceeded, cursor moves only
on success, outage metrics/slow-mode/no message loss, and the injected-clock
7-day purge.

### Step 8 — Giles

Files: `pilgrim/chat/giles.py`, `pilgrim/tests/test_chat_giles.py`.

- `enforce_signoff(text, name)`: count `"outstanding in my field"`
  case-insensitively; 0 ⇒ append the canon sentence; ≥ 1 ⇒ drop every sentence
  containing it and append the canon sentence once. Final text contains it
  exactly once and ends with
  `"This is {name} signing off with a reminder that I'm outstanding in my field."`
- `Giles.maybe_dispatch()`: paused/cooldown check, gather non-deleted,
  non-Giles human messages since the last dispatch; active summary or idle
  fictional bulletin; `LLM.chat_json(models.chat_giles, prompt, user)`; clamp
  to `giles_max_chars`; write the dispatch as a stream message with
  `user_id="giles"`, `giles=true`, Postgres row `status='accepted'`, publish,
  set `giles:last` and `giles:state`.
- Never quotes deleted or moderated text; never fabricates human messages.

*Done when* the three fake outputs (none/once/twice) all end canon and contain
the phrase exactly once; idle vs active selection, cooldown, pause and
deleted-message exclusion are tested.

### Step 9 — Web client (RADIO §9.1)

Files: `pilgrim/web/index.html`, `pilgrim/web/chat.js`, `pilgrim/web/style.css`,
`pilgrim/tests/e2e/chat.spec.ts`, `pilgrim/tests/e2e/e2e_server.py`.

- New `<section class="window sheep" data-win="sheep">` between `requests` and
  `hit`, `chat.js?v=YYYYMMDDx` cache-buster. Name row, log, compose input,
  character counter (200), send, connection status. Input disabled until a
  name is claimed. Giles messages get a distinct label/style. All user text
  via `textContent`.
- `chat.js`: token from `PD_VOTER.id()`, last stream id in `localStorage`,
  hello/welcome, `message`/`ack`/`deleted`/`warned`/`banned`/`slow_mode`/
  `error`, ping→pong, reconnect with jitter from
  `chat.reconnect_min_s`…`max_s`, replay from `last_id` without duplicates.
  Mobile-first, no framework, no build step; must not touch `player.js`.
- e2e server: build an in-process `FakeAsyncRedis` + `ChatStore` + hub, register
  chat routes, seed two names, and expose test hooks for moderation/Giles.

*Done when* `make e2e` passes desktop + 320/360 px specs: read-only before
naming, claim/change, two participants send/receive, validation/rate errors,
reconnect/replay without duplicates, deletion propagation, Giles labelled with
one canon sign-off, and PLAY/STOP + requests/voting still work.

### Step 10 — Mac worker entry point

Files: `pilgrim/chat_worker.py`, `pilgrim/deploy/mac/chat_worker.sh`,
`docs/backends.md §13`, `pilgrim/tests/test_chat_worker.py`.

- `python -m pilgrim.chat_worker`: loads config, opens the store
  (`db.open_store`), connects async Valkey from `REDIS_URL`, builds
  `Reviewer`/`Giles`/`AuditLog`, runs a loop with an injected `Clock`:
  review every `review_interval_s`, Giles every `giles_interval_s`, purge once
  a day, heartbeat `pilgrim:{chat}:worker:heartbeat` (TTL 120). SIGTERM
  finishes the batch in hand before exiting. Every dependency failure is
  logged and retried with backoff; the process never touches playout.
- launchd unit beside the station (`origin.sh` pattern), logs to
  `pilgrim/logs/chat_worker.log`; documented install/status commands.

*Done when* `test_chat_worker.py` drives one tick of each loop with fakes
(cursor advances, Giles dispatch appears, heartbeat written, purge runs) and
the worker exits cleanly on SIGTERM mid-batch without moving the cursor.

### Step 11 — Observability, terraform, RADIO.md

Files: `RADIO.md` (§§9.1, 11, 12, 14, 15), `AGENTS.md` (active-plan line),
`docs/backends.md` (§13 worker/audit), and in
`~/projects/terraform-aws-pilgrim/modules/ec2`: `ecs.tf`, `variables.tf`,
`chat.tf` (audit log group), plus a `chat_log_retention_days` variable
(default 7).

- Scoped `logs:CreateLogStream`/`PutLogEvents`/`DescribeLogStreams` on the
  audit log group ARN in `tasks_iam_role_statements`; prefer scoping
  `pilgrim-logs-read-write` down from `logs:*` at the same time.
- RADIO §12 records the `chat:` keys, `models.chat_*`, `logging.format`, and
  that `chat.retention_days` = `chat_log_retention_days` = 7. RADIO §14
  records the four tables and the key contract. RADIO §11 records the three
  routes and the frame protocol. RADIO §9.1 records the pane. RADIO §15
  records the new fakes and tests.

*Done when* `make lint`, `make test`, `make sim`, `make e2e`, `make pg-test`,
`make deploy-check` pass, terraform `fmt`/`validate` pass, and the RADIO.md
sections match the shipped code in the same commit (hard rule 8).

### Step 12 — Release, verification and handoff (SHEEP_PEN §14)

Only after the operator authorizes deployment. Record the app commit, the
effective terraform inputs/workspace, current `ecs_release`, task definition
ARN and running image digest; retain the previous image by digest. Keep posting
disabled until the worker and admin controls are verified. Do not touch
`ecs_count`, autoscaling or the ECPU cap.

1. `make lint`, `make test`, `make sim`, `make e2e`, `make deploy-check`,
   `make pg-test`, `make image-smoke`; report pass/fail counts.
2. `make image` (`linux/arm64`, `web` target), tag and push to the registry in
   `ecs_container_image`; record the pushed digest and verify the tag resolves
   to it. Never deploy the `smoke` target.
3. In `~/projects/terraform-aws-pilgrim/modules/ec2`: bump `ecs_release`
   (which only changes `dockerLabels.release`, creating a task-def revision),
   `terraform fmt`/`validate`, save and inspect a plan, apply it; stop for
   unexpected destructive changes.
4. Prove the release: service stability, intended task definition, release
   label, running `imageDigest`, desired/running count, healthy ALB targets, no
   failed deployment, public health, deployed assets, WebSocket handshake,
   reconnect/replay through the rollout.
5. Install and verify the Mac worker (launchd), its env, heartbeat, Valkey
   connectivity, cursor/backlog, audit delivery, Giles cadence. Live validation
   must not trigger song generation.
6. Recovery: disable posting/Giles, stop the worker, redeploy the retained
   previous image by digest through a new `ecs_release`. Keep new tables/audit
   data; prove old-version compatibility locally.
7. Repeat non-destructive acceptance against https://pilgrim.farm in a **fresh
   private context** (§12), checking asset versions, cache headers, WebSocket
   traffic, console errors and screenshots. Public posting/admin mutations
   only with explicit authorization.

## Verification commands

```
make lint
make test
make sim
make e2e
make pg-test
make deploy-check
make image-smoke          # container; no real backends
# live-only, explicit authorization:
make smoke SMOKE_ARGS=--gen
make media-smoke
```

For standalone/split fixtures set `PILGRIM_ENV_FILE=` and unset inherited
`DATABASE_URL`/`REDIS_URL`/backend credentials; fixtures must use a temp
SQLite/throwaway Postgres and fakes, never production state. `make
split-smoke`/`make e2e-split` exercise the retired EC2 edge and are not part of
this change.

## Rollback

- Posting off: `POST /api/admin/chat/posting {"enabled": false}` (or the
  `pilgrim:{chat}:posting_disabled` key) — radio unaffected.
- Giles off: `POST /api/admin/chat/giles {"paused": true}`.
- Worker off: `launchctl bootout` the chat-worker unit; moderation stops, Layer
  A still screens at accept time, the backlog metric grows and alarms.
- Web tier rollback: push the retained previous image digest, bump
  `ecs_release`, apply. Reverting only the release number while a mutable tag
  still points at the new bytes does not restore the old image.
- Keep the four new tables and audit data on rollback (old code ignores them).
  Do not drop the chat audit log group; it ages out at 7 days.

## Open questions for the operator

1. **TUI bindings.** The plan adds the full `/api/admin/chat/*` surface behind
   `require_dj`; the Textual console has no chat screen today. Should chat
   admin actions get TUI bindings now, or is the HTTP surface sufficient until
   a chat screen exists?
2. **`ecs_count` / ECPU sizing.** Deferred per §0.6.7. When you scale, the
   measured per-listener command rate (heartbeat `SET` every 5 s plus chat) is
   the number to size `ecpu_per_second.maximum` from; this release only exposes
   the metric.
3. **Audit log group name.** The plan uses `/aws/pilgrim/chat-audit`; confirm
   or supply the preferred name before terraform apply.
4. **Name-change cap.** 5 per 24 h is the plan's default
   (`chat.name_changes_per_day`); confirm it is strict enough for the
   impersonation concern in §4.

## Drift log (dated facts checked against the checkout, 2026-11-07)

- No drift found for §0.1: `web_app.create_web_app`, `_add_public_routes`
  (`server.py:1052`), `_add_admin_routes`, `PublicSurface._client_ip`,
  `voter.js`, `hotstate.HotState`, `FakeRedis`, `LLM.chat_json`,
  `Moderation.moderate`, `request_filter.prefilter`, `_make_rate_limiter`,
  `logging_setup`, `Store._init_schema`, `config.yaml` + `config.py` all match.
- §0.2 holds: `prompts/field.md` has no "pun in the body" line, and
  `VoicePipeline.CATCH_PHRASE` still guards only the missing case.
- §0.3/§0.4/§0.5 hold as recorded; the terraform checkout was not re-probed
  during planning (the brief forbids calling AWS while planning). The terraform
  changes in Step 11 must be verified against
  `~/projects/terraform-aws-pilgrim/modules/ec2` before apply.
