"""Request votes and song ratings (RADIO §14; docs/shipped/voting.md).
"""
from __future__ import annotations

from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    _now_iso,
)


class VotesStore(StoreBase):
    # ------------------------------------------- community votes (docs/shipped/voting.md)
    def request_score(self, request_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(value), 0) FROM request_votes "
                "WHERE request_id=?", (request_id,)).fetchone()
        return int(r[0]) if r else 0

    def cast_request_vote(self, request_id: int, voter_id: str,
                          ip: str | None, value: int, *,
                          max_voters_per_ip: int) -> dict[str, Any]:
        """Upsert one vote (docs/shipped/voting.md V2/V3/V4/V5). One locked transaction:
        window + paid + IP-cap checks and the write are atomic, so the row
        cannot move between check and insert.

        Returns {ok: True, score, mine, open} or {ok: False, code} where code
        is not_found | not_votable | voting_closed | too_many_voters."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, status, priority FROM requests WHERE id=?",
                (request_id,)).fetchone()
            if row is None:
                return {"ok": False, "code": "not_found"}
            if int(row["priority"]):
                return {"ok": False, "code": "not_votable"}
            if row["status"] not in ("queued", "producing", "ready"):
                return {"ok": False, "code": "voting_closed"}
            if ip:
                n = self._conn.execute(
                    "SELECT COUNT(DISTINCT voter_id) FROM request_votes "
                    "WHERE request_id=? AND ip=? AND voter_id<>?",
                    (request_id, ip, voter_id)).fetchone()[0]
                if int(n) >= max_voters_per_ip:
                    return {"ok": False, "code": "too_many_voters"}
            now = _now_iso()
            if value == 0:
                self._conn.execute(
                    "DELETE FROM request_votes WHERE request_id=? AND voter_id=?",
                    (request_id, voter_id))
            else:
                self._conn.execute(
                    "INSERT INTO request_votes (request_id, voter_id, ip, value, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(request_id, voter_id) DO UPDATE SET "
                    "value=excluded.value, ip=excluded.ip, "
                    "updated_at=excluded.updated_at",
                    (request_id, voter_id, ip, value, now, now))
            self._conn.commit()
            score = int(self._conn.execute(
                "SELECT COALESCE(SUM(value), 0) FROM request_votes "
                "WHERE request_id=?", (request_id,)).fetchone()[0])
        return {"ok": True, "score": score, "mine": value, "open": True}

    def cast_song_rating(self, *, item_id: int, epoch: str, seq: int,
                         voter_id: str, ip: str | None, stars: int,
                         max_voters_per_ip: int) -> dict[str, Any]:
        """Upsert one rating for an airing (docs/shipped/voting.md V2/V3). The window is
        checked by the caller (it is scheduler state, not DB state); this only
        enforces uniqueness and the per-target IP cap."""
        with self._lock:
            if ip:
                n = self._conn.execute(
                    "SELECT COUNT(DISTINCT voter_id) FROM song_ratings "
                    "WHERE epoch=? AND seq=? AND ip=? AND voter_id<>?",
                    (epoch, seq, ip, voter_id)).fetchone()[0]
                if int(n) >= max_voters_per_ip:
                    return {"ok": False, "code": "too_many_voters"}
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO song_ratings (item_id, epoch, seq, voter_id, ip, "
                "stars, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(epoch, seq, voter_id) DO UPDATE SET "
                "stars=excluded.stars, ip=excluded.ip, "
                "updated_at=excluded.updated_at",
                (item_id, epoch, seq, voter_id, ip, stars, now, now))
            self._conn.commit()
            agg = self._conn.execute(
                "SELECT COUNT(*), AVG(stars) FROM song_ratings WHERE item_id=?",
                (item_id,)).fetchone()
        count = int(agg[0] or 0)
        avg = round(float(agg[1]), 2) if agg[1] is not None else None
        return {"ok": True, "item_id": item_id, "count": count,
                "avg": avg, "mine": stars}

    def song_rating(self, item_id: int) -> dict[str, Any]:
        """Lifetime aggregate over every airing of `item_id` (docs/shipped/voting.md V10)."""
        with self._lock:
            agg = self._conn.execute(
                "SELECT COUNT(*), AVG(stars) FROM song_ratings WHERE item_id=?",
                (item_id,)).fetchone()
        count = int(agg[0] or 0)
        avg = round(float(agg[1]), 2) if agg[1] is not None else None
        return {"count": count, "avg": avg}

    def song_ratings_for(self, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Lifetime aggregates for several songs in one query (docs/shipped/ratings.md
        Step 2; docs/shipped/voting.md V10): {item_id: {"count": n, "avg": float|None}}.
        Ids with no ratings are absent; callers default to count 0 / avg None.
        One round trip per programme response, not one per song (the store
        may be Postgres across the tailnet)."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, COUNT(*) AS n, AVG(stars) AS avg_stars "
                f"FROM song_ratings WHERE item_id IN ({marks}) GROUP BY item_id",
                ids).fetchall()
        return {int(r["item_id"]): {
                    "count": int(r["n"]),
                    "avg": round(float(r["avg_stars"]), 2)
                           if r["avg_stars"] is not None else None}
                for r in rows}

    def recent_votes(self, limit: int = 100) -> list[dict[str, Any]]:
        """Admin audit trail. Deliberately omits voter_id and ip (they are
        pseudonymous identifiers; the DB keeps them for the abuse cap only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id, value, created_at, updated_at "
                "FROM request_votes ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_ratings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, epoch, seq, stars, created_at, updated_at "
                "FROM song_ratings ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def buried_requests(self, bury_threshold: int,
                        limit: int = 100) -> list[dict[str, Any]]:
        """Free queued rows at/below `bury_threshold` (admin audit)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, created_at, score FROM ("
                "SELECT id, text, created_at, "
                "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                "WHERE v.request_id = requests.id) AS score "
                "FROM requests WHERE status='queued' AND priority=0) AS sub "
                "WHERE score <= ? ORDER BY score ASC, id ASC LIMIT ?",
                (int(bury_threshold), limit)).fetchall()
        return [dict(r) for r in rows]
