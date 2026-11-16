"""Listener requests and their lifecycle (RADIO §6.1, §14).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    NOT_BENCH,
    QUEUE_ORDER,
    QUEUE_ORDER_R,
    _iso_at,
    _now_iso,
    _project_queue_row,
    _project_recent_row,
    _queued_where,
)


class RequestsStore(StoreBase):
    # -------------------------------------------------------------- requests
    def add_request(self, text: str, cap: int | None, status: str = "queued",
                    reason: str | None = None, priority: int = 0) -> dict[str, Any]:
        """Insert a listener request, then apply FIFO eviction. If more than
        `cap` are queued, the oldest FREE ones fall out (marked 'evicted').
        producing/ready/failed and paid rows are never evicted (OVERHAUL 4.4,
        docs/shipped/payments.md §3.2). `cap=None` disables eviction (the public line: every
        accepted request gets made). Returns the stored row."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, reason, created_at, updated_at, priority) "
                "VALUES (?,?,?,?,?,?) RETURNING id",
                (text, status, reason, _now_iso(), _now_iso(), priority))
            rid = int(cur.fetchone()[0])
            self._conn.commit()
        if cap is not None:
            self._evict_overflow(cap)
        r = self.get_request(rid)
        assert r is not None
        return r

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(r) if r else None

    def _evict_overflow(self, cap: int) -> None:
        """FIFO: when FREE queued requests exceed the cap, the oldest fall out
        ('ass end' drops) so the queue always holds the latest `cap`. Only
        `queued`, `priority=0` rows are evicted — never producing/ready/failed
        and never a paid request (OVERHAUL 4.4, docs/shipped/payments.md §3.2)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status='queued' AND priority=0 "
                "ORDER BY id ASC").fetchall()
            if len(rows) > cap:
                drop = len(rows) - cap
                ids = [r[0] for r in rows[:drop]]
                for i in ids:
                    self._conn.execute(
                        "UPDATE requests SET status='evicted', reason='fifo over cap' "
                        "WHERE id=?", (i,))
                self._conn.commit()

    def queued_requests(self, cap: int | None = None, *,
                        bury_threshold: int | None = None) -> list[dict[str, Any]]:
        """Live queue in queue order (paid first, in flight next, then free by
        vote score, then FIFO). Rejected/evicted/aired/... requests are not part
        of the air-able queue. `bury_threshold` skips free queued rows at/below
        that score (docs/shipped/voting.md V7); None disables burying."""
        where, params = _queued_where(bury_threshold)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER}",
                params).fetchall()
        out = [dict(r) for r in rows]
        return out[:cap] if cap else out

    def oldest_queued_request(self) -> dict[str, Any] | None:
        """Oldest still-queued request (the next one to be serviced on air)."""
        return self.next_request_to_produce()

    # --------------------------------------------------- request lifecycle (4.4)
    def next_request_to_produce(self, *,
                                bury_threshold: int | None = None
                                ) -> dict[str, Any] | None:
        """Next `queued` request in queue order (paid first, in flight next,
        then free by vote score, then FIFO). Buried free rows are skipped."""
        where, params = _queued_where(bury_threshold)
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER} LIMIT 1",
                params).fetchone()
        return dict(r) if r else None

    def mark_request_producing(self, request_id: int) -> str | None:
        """Claim the row for production. `claimed_at` is set the first time
        only (COALESCE): docs/shipped/voting.md V6 orders in-flight work by claim time, so a
        retry after an outage keeps its original place. Returns claimed_at."""
        with self._lock:
            now = _now_iso()
            self._conn.execute(
                "UPDATE requests SET status='producing', updated_at=?, "
                "claimed_at=COALESCE(claimed_at, ?) WHERE id=?",
                (now, now, request_id))
            self._conn.commit()
            r = self._conn.execute(
                "SELECT claimed_at FROM requests WHERE id=?", (request_id,)).fetchone()
        return r["claimed_at"] if r else None

    def mark_request_writing(self, request_id: int, at: float | None = None) -> None:
        """The request got its turn at the brief LLM and lyrics/brief writing
        begins: start the board's Prep Time clock (RADIO §11). Set again on a
        retry so a request that sat out an outage or a failed attempt does not
        count that idle time as prep.

        `at` re-anchors the clock when the brief was written ahead of a render
        slot: the producer passes the moment the song model started minus the
        brief's own duration, so the wait for a slot is not counted as prep."""
        now = _iso_at(at)
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET prep_started_at=?, updated_at=? WHERE id=?",
                (now, _now_iso(), request_id))
            self._conn.commit()

    def mark_request_ready(self, request_id: int, song_item_id: int | None,
                           intro_item_id: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='ready', song_item_id=?, intro_item_id=?, "
                "updated_at=? WHERE id=?",
                (song_item_id, intro_item_id, _now_iso(), request_id))
            self._conn.commit()

    def mark_request_due(self, request_id: int, due_wall: float) -> None:
        """The request's song was teed up (committed or spliced into the
        programme): record the wall time its slot airs (RADIO §11). Stamped
        again if the song is re-committed; never cleared."""
        due = datetime.fromtimestamp(float(due_wall), UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET due_at=?, updated_at=? WHERE id=?",
                (due, _now_iso(), request_id))
            self._conn.commit()

    def mark_request_aired(self, request_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='aired', aired_at=?, updated_at=? WHERE id=?",
                (_now_iso(), _now_iso(), request_id))
            self._conn.commit()

    def request_failed_attempt(self, request_id: int, max_attempts: int = 3) -> int:
        """Record a failed production attempt. Back to `queued` until
        `max_attempts`, then `failed` (head-of-line blocking disappears — the
        next request can be produced; OVERHAUL 4.4). Returns the new attempt #."""
        with self._lock:
            r = self._conn.execute(
                "SELECT attempts FROM requests WHERE id=?", (request_id,)).fetchone()
            n = int(r[0] if r and r[0] else 0) + 1
            status = "failed" if n >= max_attempts else "queued"
            self._conn.execute(
                "UPDATE requests SET attempts=?, status=?, updated_at=? WHERE id=?",
                (n, status, _now_iso(), request_id))
            self._conn.commit()
        return n

    def requeue_request(self, request_id: int) -> None:
        """Backend unreachable (transport error): back to `queued` WITHOUT
        spending an attempt — an outage must not burn listener requests."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE id=? AND status='producing'", (_now_iso(), request_id))
            self._conn.commit()

    def pending_requests(self, *,
                         bury_threshold: int | None = None) -> list[dict[str, Any]]:
        """Requests still awaiting air (queued, producing, or ready) in queue
        order. The scheduler holds the committed window short while one of these
        is uncommitted so it can air as soon as its song + intro are ready.
        Buried free rows are excluded (docs/shipped/voting.md V7): nothing is produced for
        them, so the window must not be held for one either."""
        where = f"status IN ('queued','producing','ready') AND {NOT_BENCH}"
        params: tuple[Any, ...] = ()
        if bury_threshold is not None:
            where += (" AND NOT (priority = 0 AND status = 'queued' AND "
                      "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                      "WHERE v.request_id = requests.id) <= ?)")
            params = (int(bury_threshold),)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} {QUEUE_ORDER}",
                params).fetchall()
        return [dict(r) for r in rows]

    def ready_request_songs(self) -> list[dict[str, Any]]:
        """`ready` requests in queue order (their songs are ready to air)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE status='ready' {QUEUE_ORDER}").fetchall()
        return [dict(r) for r in rows]

    def count_paid_aired_since(self, since_iso: str) -> int:
        """Priority (paid) requests that aired at/after `since_iso`. Feeds the
        Tree Fiddy Report; never names or emails."""
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE priority=1 AND status='aired' "
                "AND aired_at IS NOT NULL AND aired_at >= ?",
                (since_iso,)).fetchone()[0])

    def recently_aired_request(self, since_iso: str) -> dict[str, Any] | None:
        """The most recently aired request at/after `since_iso` (wall ISO), if
        any. Used for the DJ's back-announce (never a name or email)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE status='aired' AND aired_at IS NOT NULL "
                "AND aired_at >= ? ORDER BY aired_at DESC LIMIT 1",
                (since_iso,)).fetchone()
        return dict(r) if r else None

    def reset_producing_to_queued(self) -> None:
        """Startup: a crash mid-generation must not strand a request in
        `producing` (OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='queued', updated_at=? "
                "WHERE status='producing'", (_now_iso(),))
            self._conn.commit()

    def request_board(self, cap: int, max_length: int = 160,
                      stages: Mapping[int, str] | None = None, *,
                      voting_enabled: bool = True,
                      bury_threshold: int | None = None) -> dict[str, Any]:
        """Board for the UI: live queue (queued+producing+ready, queue order,
        <= cap) plus the 5 most recent aired requests with song titles and how
        long they waited. Rows are projected — never raw (docs/shipped/payments.md §3.4).

        Queue rows carry `id`, `score`, `votable` and `buried` (docs/shipped/voting.md §1);
        recent rows never do (no retrospective voting)."""
        with self._lock:
            queue = [dict(r) for r in self._conn.execute(
                "SELECT r.*, i.title AS song_title FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('queued','producing','ready') "
                "AND COALESCE(r.bench, 0)=0 "
                f"{QUEUE_ORDER_R} LIMIT ?", (cap,)).fetchall()]
            scores = {int(r[0]): int(r[1]) for r in self._conn.execute(
                "SELECT request_id, COALESCE(SUM(value), 0) FROM request_votes "
                "GROUP BY request_id").fetchall()}
            recent = [dict(r) for r in self._conn.execute(
                "SELECT r.id, r.text, r.summary, r.created_at, r.aired_at, "
                "r.prep_started_at, r.due_at, r.priority, "
                "i.title AS song_title, i.artist AS song_artist FROM requests r "
                "LEFT JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status IN ('aired','serviced') "
                "ORDER BY r.id DESC LIMIT 5").fetchall()]
        out = []
        for r in queue:
            rid = int(r["id"])
            paid = bool(r.get("priority"))
            score = scores.get(rid, 0)
            buried = (bury_threshold is not None and not paid
                      and r["status"] == "queued" and score <= bury_threshold)
            out.append(_project_queue_row(
                r, max_length, stages, score=score,
                votable=bool(voting_enabled and not paid),
                buried=bool(buried)))
        return {
            "queue": out,
            "recent": [_project_recent_row(r, max_length) for r in recent],
        }

    def mark_serviced(self, request_id: int) -> None:
        """Legacy alias: marks the request as read on air (recorded as `aired`
        so reads treat it as finished; OVERHAUL 4.4)."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status='aired', reason='serviced on air', "
                "aired_at=?, updated_at=? WHERE id=?",
                (_now_iso(), _now_iso(), request_id))
            self._conn.commit()
