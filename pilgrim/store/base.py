"""The connection, the schema and the key/value table (RADIO §14).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pilgrim.db import Conn, PostgresConn, SqliteConn, StoreLock


class StoreBase:
    """One database connection (SQLite file or Postgres DSN). All access goes
    through `_lock`; writes commit explicitly."""

    def __init__(self, db_path: Path | None = None, *, dsn: str | None = None,
                 password: str | None = None) -> None:
        self._conn: Conn
        if dsn:
            self.path: Path | None = None
            self._conn = PostgresConn(dsn, password)
        else:
            if db_path is None:
                raise ValueError("Store needs a SQLite path or a Postgres dsn")
            self.path = Path(db_path)
            self._conn = SqliteConn(self.path)
        self._lock = StoreLock(self._conn)
        # Read-through cache for `get_item` (docs/pending/scalability.md Step 2 follow-up):
        # the programme snapshot, the state and the DJ views look items up by
        # id many times a second, and on Postgres each miss is a tailnet round
        # trip. Every write to `items` in this class clears it (writes are a
        # few a minute); rows are returned as copies. Bounded by ITEM_CACHE_MAX.
        self._item_cache: dict[int, tuple[dict[str, Any], float]] = {}
        self._init_schema()

    @property
    def dialect(self) -> str:
        return self._conn.dialect

    @property
    def IntegrityError(self) -> type[Exception]:  # noqa: N802 - mirrors the DB-API name
        return self._conn.IntegrityError

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- song|commercial|liner|dj_talk|news|station_id|emergency
                    type TEXT NOT NULL,
                    media_path TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    title TEXT,
                    artist TEXT,
                    genre TEXT,
                    sample_rate INTEGER,
                    channels INTEGER,
                    role TEXT,
                    evergreen INTEGER DEFAULT 0,
                    emergency INTEGER DEFAULT 0,
                    fresh INTEGER DEFAULT 1,       -- 1 = unaired
                    expires_at TEXT,
                    gravity TEXT,                  -- serious|normal
                    meta_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS program (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS airplay (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    item_type TEXT,
                    started_at REAL,
                    position REAL,
                    underrun INTEGER DEFAULT 0,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    -- queued|producing|ready|aired|rejected|evicted|failed (OVERHAUL 4.4)
                    status TEXT NOT NULL DEFAULT 'queued',
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
                CREATE INDEX IF NOT EXISTS idx_items_fresh ON items(fresh);
                CREATE INDEX IF NOT EXISTS idx_program_seq ON program(seq);
                CREATE INDEX IF NOT EXISTS idx_airplay_item ON airplay(item_id);
                CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
                -- Persistent unique-visitor dedup (RADIO §14): raw canonical
                -- client IPs. Supersedes the HMAC-only visitor_signatures table.
                CREATE TABLE IF NOT EXISTS visitor_ips (
                    ip TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL DEFAULT (strftime('%s','now'))
                );
                -- DJ console (docs/systems/console.md §4): small key/value store for counters
                -- like catalogue_revision (bumped when the song catalogue set
                -- changes so the console can detect a stale page).
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                -- Stripe webhook idempotency (docs/shipped/payments.md §3.1): one row per
                -- event id; `outcome` is applied|ignored|mismatch|duplicate.
                CREATE TABLE IF NOT EXISTS stripe_events (
                    event_id    TEXT PRIMARY KEY,
                    type        TEXT NOT NULL,
                    session_id  TEXT,
                    outcome     TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                -- Request-queue votes (docs/shipped/voting.md V2): one row per voter and
                -- request, value -1|+1 (0 = retracted -> row deleted). The
                -- canonical client IP is stored for the per-target abuse cap
                -- (V3), like visitor_ips (RADIO §14).
                CREATE TABLE IF NOT EXISTS request_votes (
                    request_id INTEGER NOT NULL,
                    voter_id   TEXT    NOT NULL,
                    ip         TEXT,
                    value      INTEGER NOT NULL,
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL,
                    PRIMARY KEY (request_id, voter_id)
                );
                CREATE INDEX IF NOT EXISTS idx_request_votes_ip
                    ON request_votes(request_id, ip);
                -- Song ratings (docs/shipped/voting.md V2, V8): one row per AIRING, keyed by
                -- (epoch, seq, voter) so the same song can be rated again the
                -- next time it airs. `item_id` feeds the lifetime aggregate.
                CREATE TABLE IF NOT EXISTS song_ratings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id    INTEGER NOT NULL,
                    epoch      TEXT    NOT NULL,
                    seq        INTEGER NOT NULL,
                    voter_id   TEXT    NOT NULL,
                    ip         TEXT,
                    stars      INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
                    created_at TEXT    NOT NULL,
                    updated_at TEXT    NOT NULL,
                    UNIQUE (epoch, seq, voter_id)
                );
                CREATE INDEX IF NOT EXISTS idx_song_ratings_item
                    ON song_ratings(item_id);
                CREATE INDEX IF NOT EXISTS idx_song_ratings_ip
                    ON song_ratings(epoch, seq, ip);
                -- Promo codes for the paid request line (2026-11-08). A code
                -- has a fixed number of uses; one redemption per identity.
                -- Exhausted codes stay in the table (active=0, retired_by
                -- 'exhausted'), never deleted, so a guessed spent code can
                -- answer "all used up" instead of "invalid".
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    code       TEXT    NOT NULL UNIQUE,
                    max_uses   INTEGER NOT NULL,
                    uses       INTEGER NOT NULL DEFAULT 0,
                    active     INTEGER NOT NULL DEFAULT 1,
                    note       TEXT,
                    created_at TEXT    NOT NULL,
                    retired_at TEXT,
                    retired_by TEXT     -- operator|exhausted
                );
                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    code_id    INTEGER NOT NULL,
                    identity   TEXT    NOT NULL,
                    request_id INTEGER,
                    created_at TEXT    NOT NULL,
                    PRIMARY KEY (code_id, identity)
                );
                CREATE INDEX IF NOT EXISTS idx_promo_redemptions_code
                    ON promo_redemptions(code_id);
                -- The Sheep Pen chat (docs/shipped/sheep-pen.md; RADIO §14). Valkey holds
                -- the hot stream, rate limits and bans; these four tables are
                -- the durable audit copy that survives the cache.
                CREATE TABLE IF NOT EXISTS chat_users (
                    user_id     TEXT PRIMARY KEY,
                    token_hash  TEXT NOT NULL UNIQUE,
                    name        TEXT,
                    name_norm   TEXT,
                    ip_hash     TEXT,
                    created_at  TEXT NOT NULL,
                    last_seen   TEXT,
                    released_at TEXT,
                    name_changed_at TEXT
                );
                -- authoritative name uniqueness: one active user per name
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_users_name_norm
                    ON chat_users(name_norm)
                    WHERE released_at IS NULL AND name_norm IS NOT NULL;
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id            TEXT PRIMARY KEY,   -- the Valkey stream id
                    user_id       TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    text          TEXT,               -- nulled after retention_days
                    ts            REAL NOT NULL,
                    client_msg_id TEXT,
                    -- pending_review|accepted|deleted|flagged
                    status        TEXT NOT NULL,
                    flags         TEXT,
                    deleted_at    TEXT,
                    deleted_by    TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_ts
                    ON chat_messages(ts);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_status
                    ON chat_messages(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_client
                    ON chat_messages(user_id, client_msg_id)
                    WHERE client_msg_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS chat_bans (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope      TEXT NOT NULL,          -- user|ip
                    target     TEXT NOT NULL,          -- user_id|ip_hash
                    until      REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    reason     TEXT,
                    active     INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_chat_bans_target
                    ON chat_bans(scope, target, active);
                CREATE TABLE IF NOT EXISTS chat_moderation (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id  TEXT NOT NULL UNIQUE,
                    message_id TEXT,
                    user_id    TEXT,
                    action     TEXT NOT NULL,
                    policy     TEXT,
                    severity   INTEGER,
                    confidence REAL,
                    reason     TEXT,
                    text       TEXT,                   -- nulled after retention_days
                    created_at TEXT NOT NULL,
                    decided_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_moderation_message
                    ON chat_moderation(message_id);
                DROP TABLE IF EXISTS visitor_signatures;
                """
            )
            # idempotent migration (OVERHAUL 2.2): retire truncated inventory
            cols = self._conn.columns("items")
            if "retired" not in cols:
                self._conn.execute(
                    "ALTER TABLE items ADD COLUMN retired INTEGER DEFAULT 0")
            # idempotent migration (PRIORITIES §4): persisted airplay so a
            # restart doesn't reset song rotation (replaces the in-memory map).
            # last_aired_at is a wall-clock epoch set at commit; NULL = never aired
            # (or aired before the ledger existed) -> treated as unheard forever.
            if "last_aired_at" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN last_aired_at REAL")
            if "play_count" not in cols:
                self._conn.execute(
                    "ALTER TABLE items ADD COLUMN play_count INTEGER DEFAULT 0")
            # idempotent migration (RADIO §8.4, §14): S3 is the media source of
            # truth. s3_key is the object key in media.bucket (NULL = not yet
            # uploaded; the API then falls back to /api/media/<id>).
            if "s3_key" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN s3_key TEXT")
            if "uploaded_at" not in cols:
                self._conn.execute("ALTER TABLE items ADD COLUMN uploaded_at TEXT")
            # idempotent migration (docs/shipped/album-art.md Step 4, RADIO §14): cover art
            # per song. art_path is the absolute local JPEG (NULL = no art yet),
            # art_s3_key the published object key (NULL = not uploaded), and
            # art_attempted_at the epoch of the last failed attempt (back-off).
            for col, ddl in (("art_path", "TEXT"), ("art_s3_key", "TEXT"),
                             ("art_uploaded_at", "TEXT"),
                             ("art_attempted_at", "REAL")):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
            # idempotent migration (OVERHAUL 4.4 + docs/shipped/payments.md §3.1): request
            # lifecycle + paid-priority columns. Additive only.
            req_cols = self._conn.columns("requests")
            for col, ddl in (("attempts", "INTEGER DEFAULT 0"),
                             ("song_item_id", "INTEGER"),
                             ("intro_item_id", "INTEGER"),
                             ("updated_at", "TEXT"),
                             ("priority", "INTEGER DEFAULT 0"),
                             ("public_token", "TEXT"),
                             ("paid_at", "TEXT"),
                             ("amount_cents", "INTEGER"),
                             ("currency", "TEXT"),
                             ("checkout_session_id", "TEXT"),
                             ("payment_intent_id", "TEXT"),
                             ("livemode", "INTEGER"),
                             ("reconciled_at", "REAL"),
                             ("aired_at", "TEXT"),
                             # board "Prep Time" starts here (first lyrics write),
                             # not at created_at (queue idle must not count)
                             ("prep_started_at", "TEXT"),
                             # wall time the song was scheduled to air when it
                             # was teed up (committed/spliced); caps Prep Time
                             # so a frozen station does not inflate it
                             ("due_at", "TEXT"),
                             ("summary", "TEXT"),
                             # docs/shipped/payments.md Step 12b: staged checks + operator comp
                             ("stage", "TEXT"),
                             ("comped", "INTEGER DEFAULT 0"),
                             # docs/shipped/voting.md V6: first time the producer claims the
                             # row, so in-flight order is the claim order
                             ("claimed_at", "TEXT"),
                             # docs/shipped/prompt-upgrade.md Step 2: the three-box paid
                             # request, kept alongside the composed `text`
                             ("lyrics_text", "TEXT"),
                             ("music_text", "TEXT"),
                             ("art_text", "TEXT"),
                             # docs/systems/queue-bench.md: a benchmark row. It
                             # uses the real queue but never airs, is never on
                             # the board and never counts; `bench_opts` is JSON
                             # (run, duration_s, replay_of, result)
                             ("bench", "INTEGER DEFAULT 0"),
                             ("bench_opts", "TEXT")):
                if col not in req_cols:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_public_token "
                "ON requests(public_token) WHERE public_token IS NOT NULL")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_session "
                "ON requests(checkout_session_id) WHERE checkout_session_id IS NOT NULL")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_queue "
                "ON requests(status, priority)")
            self._conn.commit()

    # -------------------------------------------------------------- key/value
    def meta_get(self, key: str) -> str | None:
        """One small key/value from the `meta` table (leases, counters)."""
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?",
                                   (key,)).fetchone()
        return None if r is None else r["value"]

    def meta_set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
            self._conn.commit()
