"""The committed program, the airplay ledger and visitors (RADIO §5, §14).
"""
from __future__ import annotations

from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    NOT_BENCH,
    _now_iso,
)


class ProgramStore(StoreBase):
    # --------------------------------------------------------------- program
    def clear_program(self) -> None:
        """Wipe the committed program. Used on station start: the committed
        program is live-only state referencing a clock that no longer exists
        (OVERHAUL 2.4)."""
        with self._lock:
            self._conn.execute("DELETE FROM program")
            self._conn.commit()

    def append_program(self, item_id: int, type_: str, duration_s: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO program (item_id, type, duration_s, committed_at) "
                "VALUES (?,?,?,?) RETURNING seq",
                (item_id, type_, duration_s, _now_iso()))
            rid = cur.fetchone()[0]
            self._conn.commit()
            return int(rid)

    def append_program_at(self, *, item_id: int, type_: str, duration_s: float,
                          seq: int) -> int:
        """Insert a committed program row at an explicit seq (operator-phrase
        head-of-queue splicing, scheduler.place_phrase). Caller must have shifted
        later rows up (``shift_program_up_from``) so ``seq`` is free."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO program (seq, item_id, type, duration_s, committed_at) "
                "VALUES (?,?,?,?,?)",
                (seq, item_id, type_, duration_s, _now_iso()))
            self._conn.sync_sequence("program", "seq")
            self._conn.commit()
        return int(seq)

    def update_program_seq(self, old_seq: int, new_seq: int) -> None:
        """Renumber one committed row (request splice). New seqs are always
        above the current high-water mark, so an old occurrence id is never
        reused for a different item (docs/systems/console.md §5)."""
        with self._lock:
            self._conn.execute(
                "UPDATE program SET seq=? WHERE seq=?", (new_seq, old_seq))
            self._conn.sync_sequence("program", "seq")
            self._conn.commit()

    def set_program_seq_high(self, seq: int) -> None:
        """Raise the seq high-water mark after a splice renumbers future rows;
        otherwise the next append could reuse a shifted seq (engine-specific:
        sqlite_sequence or the identity sequence, see pilgrim.db)."""
        with self._lock:
            self._conn.set_sequence_high("program", "seq", int(seq))
            self._conn.commit()

    def shift_program_up_from(self, seq: int) -> None:
        """Shift all committed rows with seq >= ``seq`` up by one, descending so
        the AUTOINCREMENT primary key never collides. Used to open a slot for a
        head-of-queue operator phrase (scheduler.place_phrase). Only future rows
        move (already-aired seqs are below the splice point and untouched), so
        airplay ledger references stay valid."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq FROM program WHERE seq >= ? ORDER BY seq DESC",
                (seq,)).fetchall()
            for (s,) in rows:
                self._conn.execute("UPDATE program SET seq=? WHERE seq=?", (s + 1, s))
            self._conn.sync_sequence("program", "seq")
            self._conn.commit()

    def program_after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.seq, p.item_id, p.type AS type, p.duration_s "
                "FROM program p WHERE p.seq>? ORDER BY p.seq", (seq,)).fetchall()
        return [dict(r) for r in rows]

    def max_seq(self) -> int | None:
        with self._lock:
            r = self._conn.execute("SELECT COALESCE(MAX(seq),0) FROM program").fetchone()
        return int(r[0]) if r and r[0] else 0

    def truncate_program_before(self, seq: int) -> None:
        """Drop committed rows older than `seq` (keeps the live window bounded)."""
        with self._lock:
            self._conn.execute("DELETE FROM program WHERE seq<?", (seq,))
            self._conn.commit()

    def delete_program_rows(self, seqs: list[int]) -> int:
        """Delete committed future rows by seq (docs/shipped/idle.md thaw revalidation).
        Deleted seqs are never reused: new rows always take seqs above the
        AUTOINCREMENT high-water mark (update_program_seq/set_program_seq_high).
        Returns the number of rows removed."""
        if not seqs:
            return 0
        with self._lock:
            marks = ",".join("?" * len(seqs))
            cur = self._conn.execute(
                f"DELETE FROM program WHERE seq IN ({marks})", tuple(seqs))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def program_since(self, since_seq: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.seq, p.item_id, p.type AS type, p.duration_s "
                "FROM program p WHERE p.seq>=? ORDER BY p.seq", (since_seq,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- airplay
    def record_airplay(self, item_id: int, seq: int, item_type: str | None,
                       started_at: float | None, position: float | None,
                       underrun: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                ("INSERT INTO airplay (item_id, seq, item_type, started_at, position, "
                 "underrun, recorded_at) VALUES (?,?,?,?,?,?,?) RETURNING id"),
                (item_id, seq, item_type, started_at, position, underrun, _now_iso()))
            rid = cur.fetchone()[0]
            self._conn.commit()
            return int(rid)

    def recent_airplay(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM airplay ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_air_type_sequence(self, program_slice: list[dict[str, Any]]) -> list[str]:
        """Map most recent committed items to their types in program order."""
        return [p["item_type"] for p in program_slice]

    # ------------------------------------------------------------ visitors (14)
    def register_visitor(self, ip: str) -> int:
        """Idempotently record a canonical client IP (insert-on-conflict no-op)
        and return the unique count, all under the store's lock so a concurrent
        first-seen IP can't double-count (RADIO §14)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO visitor_ips (ip) VALUES (?) ON CONFLICT(ip) DO NOTHING", (ip,))
            n = self._conn.execute(
                "SELECT COUNT(*) FROM visitor_ips").fetchone()[0]
            self._conn.commit()
        return int(n)

    def unique_visitors(self) -> int:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM visitor_ips").fetchone()[0]
        return int(n)

    def request_counts(self) -> tuple[int, int]:
        """(paid, total) listener requests for the HIT COUNTER pane
        (RADIO §9.1, §14). `paid` counts priority rows that actually became
        paid (Stripe confirmed or an operator comp, both of which set
        `paid_at`); rows still in checkout (`checking`, `awaiting_payment`) or
        `abandoned`/`rejected` before payment never count as paid. `total` is
        every request row ever stored, free or paid."""
        with self._lock:
            paid = self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE priority=1 "
                f"AND paid_at IS NOT NULL AND {NOT_BENCH}").fetchone()[0]
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM requests WHERE {NOT_BENCH}").fetchone()[0]
        return int(paid), int(total)
