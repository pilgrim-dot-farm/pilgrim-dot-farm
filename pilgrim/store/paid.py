"""Paid priority requests and queue-benchmark rows (RADIO §11, §14).
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from pilgrim.pipelines.request_parts import RequestParts
from pilgrim.store.common import (
    NOT_BENCH,
    QUEUE_ORDER,
    _now_iso,
    _part_values,
)
from pilgrim.store.requests import RequestsStore


class PaidStore(RequestsStore):
    # ------------------------------------------------- paid priority (PAYMENT §3)
    def add_paid_request(self, text: str, summary: str | None = None,
                         parts: RequestParts | None = None) -> dict[str, Any]:
        """Insert a moderated paid request: awaiting payment, priority 1,
        unguessable public token, plus the board summary. Not in any queue
        until Stripe confirms.

        docs/shipped/prompt-upgrade.md Step 2: the three box columns are NULL for a legacy
        row, and `""` for a box the listener left empty."""
        token = secrets.token_urlsafe(24)
        lyrics, music, art = _part_values(parts)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, created_at, updated_at, "
                "priority, public_token, summary, lyrics_text, music_text, "
                "art_text) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (text, "awaiting_payment", _now_iso(), _now_iso(), 1, token,
                 summary, lyrics, music, art))
            rid = int(cur.fetchone()[0])
            self._conn.commit()
        row = self.get_request(rid)
        assert row is not None
        return row

    def waiting_free_requests(self) -> int:
        """Free requests not yet aired (queued/producing/ready): the number the
        free line caps at `requests.free_queue_max`. Paid rows never count."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE COALESCE(priority, 0)=0 "
                f"AND status IN ('queued','producing','ready') AND {NOT_BENCH}").fetchone()
        return int(r[0])

    def paid_rows_needing_summary(self) -> list[dict[str, Any]]:
        """Paid rows the board can show (waiting, or among the aired) —
        candidates for a summary regenerated after the 512-token cap left
        raw-text labels (Oct 31)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE priority=1 AND status IN "
                "('queued','producing','ready','aired') "
                "ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def set_request_summary(self, request_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE requests SET summary=?, updated_at=? WHERE id=?",
                               (summary, _now_iso(), request_id))
            self._conn.commit()

    def add_checking_request(self, text: str, *, comped: bool = False,
                             parts: RequestParts | None = None
                             ) -> dict[str, Any]:
        """Insert a prefiltered paid request whose checks run in the background
        (docs/shipped/payments.md Step 12b): status `checking`, stage `moderation`, priority 1,
        unguessable public token. Not in any queue, not on the board.

        docs/shipped/prompt-upgrade.md Step 2: the three parts are stored as `""` when the
        listener left a box empty, so `from_row` can tell a three-box request
        from a legacy one."""
        token = secrets.token_urlsafe(24)
        lyrics, music, art = _part_values(parts)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, stage, created_at, updated_at, "
                "priority, public_token, comped, lyrics_text, music_text, "
                "art_text) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (text, "checking", "moderation", _now_iso(), _now_iso(), 1, token,
                 1 if comped else 0, lyrics, music, art))
            rid = int(cur.fetchone()[0])
            self._conn.commit()
        row = self.get_request(rid)
        assert row is not None
        return row

    def set_request_stage(self, request_id: int, stage: str,
                          summary: str | None = None) -> bool:
        """Advance a `checking` row to its next stage (moderation -> copyright
        -> ready). False when the row is no longer checking."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET stage=?, summary=COALESCE(?, summary), "
                "updated_at=? WHERE id=? AND status='checking'",
                (stage, summary, _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def reject_checking_request(self, request_id: int, reason: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='rejected', reason=?, stage=NULL, "
                "updated_at=? WHERE id=? AND status='checking'",
                (reason, _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def claim_for_checkout(self, request_id: int) -> bool:
        """`checking`/`ready` -> `awaiting_payment`, exactly once, so a double
        click cannot create two Checkout Sessions. Comped rows never pay."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='awaiting_payment', stage=NULL, "
                "updated_at=? WHERE id=? AND status='checking' AND stage='ready' "
                "AND COALESCE(comped, 0)=0", (_now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_request_comped(self, request_id: int) -> bool:
        """Operator comp (docs/shipped/payments.md Step 12b): a checked `comped` row joins
        the priority queue with no Stripe session and amount 0."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', stage=NULL, paid_at=?, "
                "amount_cents=0, livemode=0, updated_at=? WHERE id=? "
                "AND status='checking' AND comped=1", (_now_iso(), _now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def abandon_checking_requests(self) -> int:
        """At startup: a `checking` row's background task died with the old
        process, so nothing will ever advance it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', stage=NULL, updated_at=? "
                "WHERE status='checking'", (_now_iso(),))
            self._conn.commit()
            return cur.rowcount

    def attach_checkout(self, request_id: int, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET checkout_session_id=?, updated_at=? WHERE id=?",
                (session_id, _now_iso(), request_id))
            self._conn.commit()

    def get_request_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE public_token=?", (token,)).fetchone()
        return dict(r) if r else None

    def get_request_by_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM requests WHERE checkout_session_id=?", (session_id,)).fetchone()
        return dict(r) if r else None

    def mark_request_paid(self, request_id: int, *, session_id: str,
                          payment_intent_id: str | None, amount_cents: int,
                          currency: str, livemode: bool) -> bool:
        """The single transition into the priority queue. Only from
        awaiting_payment/abandoned, and only for the matching session. True
        only when a row actually changed (idempotent)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='queued', paid_at=?, amount_cents=?, "
                "currency=?, payment_intent_id=?, livemode=?, updated_at=? "
                "WHERE id=? AND checkout_session_id=? "
                "AND status IN ('awaiting_payment','abandoned')",
                (_now_iso(), amount_cents, currency, payment_intent_id,
                 1 if livemode else 0, _now_iso(), request_id, session_id))
            self._conn.commit()
            return cur.rowcount > 0

    def mark_request_abandoned(self, request_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', updated_at=? "
                "WHERE id=? AND status='awaiting_payment'", (_now_iso(), request_id))
            self._conn.commit()
            return cur.rowcount > 0

    def abandon_stale_checkouts(self, older_than_iso: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE requests SET status='abandoned', updated_at=? "
                "WHERE status='awaiting_payment' AND created_at < ?",
                (_now_iso(), older_than_iso))
            self._conn.commit()
            return cur.rowcount

    def record_stripe_event(self, event_id: str, type_: str, session_id: str | None,
                            outcome: str) -> bool:
        """Insert-or-ignore; False means this event id was already seen."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO stripe_events "
                "(event_id, type, session_id, outcome, received_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (event_id, type_, session_id, outcome, _now_iso()))
            self._conn.commit()
            return cur.rowcount > 0

    def update_stripe_event_outcome(self, event_id: str, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE stripe_events SET outcome=? WHERE event_id=?", (outcome, event_id))
            self._conn.commit()

    def touch_reconciled(self, request_id: int, at: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET reconciled_at=? WHERE id=?", (at, request_id))
            self._conn.commit()

    def request_position(self, request_id: int) -> int | None:
        """1-based place in the combined live queue, else None."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM requests WHERE status IN ('queued','producing','ready') "
                f"{QUEUE_ORDER}").fetchall()
        for index, row in enumerate(rows, start=1):
            if row[0] == request_id:
                return index
        return None

    def paid_requests(self, statuses: list[str], limit: int = 50) -> list[dict[str, Any]]:
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE priority=1 AND status IN ({marks}) "
                "ORDER BY id DESC LIMIT ?", (*statuses, limit)).fetchall()
        return [dict(r) for r in rows]

    def mismatch_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stripe_events WHERE outcome='mismatch' "
                "ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def all_requests(self, limit: int = 500) -> list[dict[str, Any]]:
        """Full request ledger (all statuses), newest first — for audit/history."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------ queue benchmark (queue-bench.md)
    def add_bench_request(self, text: str, *, paid: bool, opts: dict[str, Any],
                          parts: RequestParts | None = None) -> dict[str, Any]:
        """Insert a benchmark request straight into the queue (RADIO §6.1).
        A paid one is shaped like an operator comp (priority 1, `paid_at` now,
        amount 0, no Stripe ids), so it sorts exactly like a real paid row."""
        now = _now_iso()
        lyrics, music, art = _part_values(parts)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests (text, status, created_at, updated_at, priority, "
                "paid_at, amount_cents, livemode, comped, lyrics_text, music_text, "
                "art_text, bench, bench_opts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "RETURNING id",
                (text, "queued", now, now, 1 if paid else 0, now if paid else None,
                 0 if paid else None, 0 if paid else None, 1 if paid else 0,
                 lyrics, music, art, 1, json.dumps(opts)))
            rid = int(cur.fetchone()[0])
            self._conn.commit()
        row = self.get_request(rid)
        assert row is not None
        return row

    def finish_bench_request(self, request_id: int, result: dict[str, Any]) -> None:
        """A benchmark request's song rendered and was thrown away: record the
        timings in `bench_opts.result` and close the row (`bench_done`)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT bench_opts FROM requests WHERE id=?", (request_id,)).fetchone()
            opts = json.loads(r["bench_opts"] or "{}") if r else {}
            opts["result"] = result
            self._conn.execute(
                "UPDATE requests SET status='bench_done', bench_opts=?, updated_at=? "
                "WHERE id=? AND bench=1", (json.dumps(opts), _now_iso(), request_id))
            self._conn.commit()

    def bench_requests(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Every benchmark row, oldest first, with its vote score."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.*, (SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                "WHERE v.request_id = r.id) AS score FROM requests r "
                "WHERE r.bench=1 ORDER BY r.id ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def request_song_brief(self, request_id: int) -> dict[str, Any] | None:
        """The brief (title, artist, genre, caption, lyrics) a past request's
        song was rendered from, or None. A bench replay renders from it again
        instead of asking the brief LLM for new words."""
        with self._lock:
            r = self._conn.execute(
                "SELECT i.meta_json FROM requests r JOIN items i ON i.id = r.song_item_id "
                "WHERE r.id=?", (request_id,)).fetchone()
        if r is None:
            return None
        brief = (json.loads(r["meta_json"] or "{}")).get("brief")
        if not isinstance(brief, dict) or not brief.get("style_prompt"):
            return None
        return {k: v for k, v in brief.items() if k not in ("seed", "request_id")}

    def replay_candidates(self, limit: int) -> list[dict[str, Any]]:
        """Newest aired listener requests that have a song: what a bench run
        can replay (text, and the words the song was made from)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.id, r.text, r.priority FROM requests r "
                "JOIN items i ON i.id = r.song_item_id "
                "WHERE r.status='aired' AND COALESCE(r.bench, 0)=0 "
                "ORDER BY r.id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": int(r["id"]), "text": str(r["text"])[:120],
                 "paid": bool(r["priority"])} for r in rows]

    def purge_bench_requests(self, *, apply: bool) -> dict[str, int]:
        """Delete benchmark rows and their votes. Queued rows go too (that is
        how a run is cancelled); a row the producer is working on right now is
        kept, so purge again once it finishes. `apply=False` only counts."""
        with self._lock:
            total = int(self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE bench=1").fetchone()[0])
            active = int(self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE bench=1 "
                "AND status = 'producing'").fetchone()[0])
            votes = int(self._conn.execute(
                "SELECT COUNT(*) FROM request_votes WHERE request_id IN "
                "(SELECT id FROM requests WHERE bench=1)").fetchone()[0])
            if apply:
                self._conn.execute(
                    "DELETE FROM request_votes WHERE request_id IN "
                    "(SELECT id FROM requests WHERE bench=1 AND status <> 'producing')")
                self._conn.execute(
                    "DELETE FROM requests WHERE bench=1 AND status <> 'producing'")
                self._conn.commit()
        return {"requests": total, "votes": votes, "producing_kept": active}
