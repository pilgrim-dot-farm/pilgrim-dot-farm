# Sheep Pen — the listener chat

> *Formerly `SHEEP_PEN.md` (the brief), `SHEEP_PEN_PLAN.md`.*

**Status: shipped** (Steps 1–10 and the docs half of Step 11, 2026-11-07;
amended 2026-11-15). What is left of Steps 11–12 is in PENDING.md.

A live chat pane on the site: WebSocket fan-out on the web tier, hot state in
Valkey, durable rows and moderation audit in Postgres, a review worker on the
Mac, and operator controls on the admin listener. Giles does not post in the
chat; he comments on the pen in his radio field report. Normative behaviour:
RADIO §6.1 ("Field reports and the pen"), §9.1, §11, §12, §14.

Code: `pilgrim/chat/` (`hub`, `routes`, `actions`, `review`, `screen`,
`prohibited`, `identity`, `redis_store`, `audit`, `protocol`),
`pilgrim/chat_worker.py`, `pilgrim/web/chat.js`,
`pilgrim/prompts/chat_moderation.md`, `field_pen.md`,
`/api/admin/chat/*` in `pilgrim/server.py`.

The brief (requirements, 1,300 lines; cited in code as `SHEEP_PEN §n`) is in
git: `git show c712f27:docs/plans/sheep-pen-brief.md`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/plans/sheep-pen-plan.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

> **Amended 2026-11-15 (operator correction).** Giles does **not** post in the
> chat. Step 8 (`pilgrim/chat/giles.py`, `prompts/field_chat.md`, the
> `chat.giles_*` / `chat.pen_reporter_name` / `models.chat_giles` keys, the
> `giles` frame flag, `POST /api/admin/chat/giles` and the `giles:*` Valkey
> keys) was built from a misreading and has been removed. What was wanted:
> Giles comments on the pen **in his radio field report** (RADIO §6.1 "Field
> reports and the pen"; `Producer._pen_chatter`, `prompts/field_pen.md`,
> `talk.field_pen_*`). Ignore every Giles-in-chat instruction below and in
> the brief (§0.2, §0.7.10, §8).

> **Read this first.** This is the executable plan derived from `docs/shipped/sheep-pen.md`
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
   TTL. **Amended 2026-11-07 after the first release: a claimed name is
   locked** (`chat.allow_name_changes: false`); only the admin rename route
   may change it, and `name_changes_per_day` was removed. Release: worker daily sweep for `released_at IS NULL` users whose
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
    `name_min`, `name_max`, `name_release_days`, `allow_name_changes`,
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
4. **Name changes.** Settled 2026-11-07: names are locked on claim
   (`chat.allow_name_changes: false`); only the operator rename route changes
   one.

## Implementation status (local, 2026-11-07)

Steps 1–10 and the RADIO/docs half of Step 11 are implemented and committed
(`9d73ab4`). Verification run locally:

| Check | Result |
|-------|--------|
| `make lint` | pass (ruff + mypy, 147 files) |
| `make test` | 916 passed |
| `make sim` | all 24-h assertions passed |
| `make e2e` | 36 passed (includes `chat.spec.ts`, 9 specs, fresh contexts) |
| `make pg-test` | 916 passed against throwaway Postgres |
| `make deploy-check` | edge + gateway nginx configs pass |
| `make image-smoke` | container smoke pass (health/programme/media/site/admin/non-root) |
| `python -m pilgrim.chat_worker` | imports; loop covered by `test_chat_worker.py` |

Two stale assertions in `pilgrim/tests/e2e/cosmetic.spec.ts` were corrected
because the pane-order check now includes `sheep`: the motivation title was
asserted as `MOTIVATION` (the shipped HTML says `CORPORATE MISSION STATEMENT`)
and the credits list was asserted as 7 links (the shipped HTML has 8, with the
Anthropic dev link). These were pre-existing drift, not weakening: the pane
order, credit count and link allowlist still assert the shipped page.

**Not done here (needs the operator/terraform checkout and explicit release
authorization):** Step 11's terraform changes (audit log group,
`chat_log_retention_days`, scoped task-role `logs:` statement) and Step 12
(image push, `ecs_release` bump, ECS rollout, Mac worker install, live
acceptance against https://pilgrim.farm). Public posting stays disabled until
the worker and admin controls are verified per SHEEP_PEN §14. The remote
Playwright MCP was not used for interactive acceptance; the repository
Playwright suite (fresh contexts, desktop + 320 px) covered the UI, per the
brief's fallback when the MCP offers no isolated context.


## Operator amendment (2026-11-07, after the first image push)

- A claimed display name is **locked** to its user. The REST/WS claim path
  refuses a different name with 409 `name_locked` (re-claiming the same name is
  idempotent); only `/api/admin/chat/name` may rename. `chat.name_changes_per_day`
  is removed; `chat.allow_name_changes` (default false) is the operator switch.
- Each display name is coloured from the standard xterm **256-colour palette**
  by a stable hash of the lowercased name. Purple/violet/magenta hues (HSV
  255–345) and entries with lightness < 0.45 or saturation < 0.25 are filtered
  out, so names stay readable on the dark pane and are not all cyan. Giles
  keeps the magenta field-report styling.
- The connection status text is **GRAZE UPON IT** (was "in the pen"); the
  operator's staged pane title is `SHEEP PEN`.

**Amendment image (2026-11-07):** commit `bdd1b72`, tags
`pilgrimdotfarm/front-end:bdd1b72` and `:latest`, manifest-list digest
`sha256:b9b9a13b0557d9c7e7f8ae6b806b569d1b519c60f9109e7fede0832a77c156be`
(arm64 manifest `sha256:337043ad85f48bc4a40a5b8053333b9da870cdc5b2a648b37ca6fd94490ddba3`).
Checks: `make lint`, 917 tests, `make sim`, 36 e2e, `make pg-test` all pass.
ECS is not yet rolled (`ecs_release` unchanged).

- **Spam cool-off (2026-11-07):** after `chat.spam_offense_limit` (5) rate or
  repeat rejections inside `chat.spam_offense_window_s` (60 s), the user is
  muted for `chat.cooldown_ladder_s[0]` (300 s); further cool-offs within 24 h
  escalate to 900 s then 3600 s. The key is `pilgrim:{chat}:cool:user:{id}`
  (TTL), audited as a `chat_moderation` row with action `cooldown` and policy
  `spam`; it is not a `chat_bans` row, so Layer B and operator bans are
  unaffected. A targeted `cooldown` frame reaches that user's sockets, the
  client disables compose until it expires, and an operator user-unban clears
  it. Existing per-user 3 s and per-IP 20/min limits stay.

**Spam cool-off image (2026-11-07):** commit `fe0ac81`, tags
`pilgrimdotfarm/front-end:fe0ac81` and `:latest`, manifest digest
`sha256:fb167e03f10ec65e70f81b704ce1a9236b32e12aabc77df4238b28e67b8d42e8`
(arm64 manifest `sha256:12dfafc8b0bddd4729ce3e88f77eec81bc4d334f831642bc615172086f6079d0`).
Checks: `make lint`, 919 tests, `make sim`, 37 e2e, `make pg-test` all pass.
ECS is not yet rolled (`ecs_release` unchanged).
