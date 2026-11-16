"""Inventory items, cover art and the DJ catalogue (RADIO §7, §8.4, §11).
"""
from __future__ import annotations

import json
import time
from typing import Any

from pilgrim.store.base import StoreBase
from pilgrim.store.common import (
    REQUEST_PRIORITY_H,
    _iso_ago,
    _now_iso,
    _parse_epoch,
)

ITEM_CACHE_MAX = 20_000
# A row older than this is re-read: another PROCESS (tools/art_backfill,
# tools/audit_library --apply, mp3_migrate) may have written `items` behind this
# store's back, and in-process invalidation cannot see that. Cosmetic fields
# (art, s3_key) may therefore lag by up to this long; flags the scheduler
# needs are written in-process and invalidate immediately.
ITEM_CACHE_TTL_S = 60.0
_cache_clock = time.time   # patched by tests


class ItemsStore(StoreBase):
    # ------------------------------------------------------------------ items
    def add_item(self, *, type_: str, media_path: str, duration_s: float,
                 title: str | None = None, artist: str | None = None,
                 genre: str | None = None, sample_rate: int | None = None,
                 channels: int | None = None, role: str | None = None,
                 evergreen: bool = False, emergency: bool = False,
                 fresh: bool = True, expires_at: str | None = None,
                 gravity: str | None = None, meta: dict | None = None,
                 created_at: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO items
                   (type, media_path, duration_s, title, artist, genre, sample_rate,
                    channels, role, evergreen, emergency, fresh, expires_at, gravity,
                    meta_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (type_, media_path, duration_s, title, artist, genre, sample_rate,
                 channels, role, 1 if evergreen else 0, 1 if emergency else 0,
                 1 if fresh else 0, expires_at, gravity,
                 json.dumps(meta) if meta else None, created_at or _now_iso()))
            rid = cur.fetchone()[0]
            if type_ == "song":
                self._bump_catalogue()
            self._items_changed()
            self._conn.commit()
            return int(rid)

    def items_to_publish(self, limit: int = 50) -> list[dict[str, Any]]:
        """MP3 items not yet in S3 (RADIO §8.4), oldest first. Emergency items
        are local-only (§8.3) and never published."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE s3_key IS NULL AND emergency=0 "
                "AND media_path LIKE '%.mp3' ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_published(self, item_id: int, s3_key: str) -> None:
        """Record a confirmed upload. Only a successful PUT calls this."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET s3_key=?, uploaded_at=? WHERE id=?",
                (s3_key, _now_iso(), item_id))
            self._items_changed()
            self._conn.commit()

    # ------------------------------------------------------------------- art
    def songs_needing_art(self, limit: int = 10, *, retry_after_s: float = 300.0,
                          now: float | None = None) -> list[dict[str, Any]]:
        """Songs with no art yet (docs/shipped/album-art.md Step 5): both title and artist
        present, not retired/emergency, and not attempted within
        `retry_after_s`.

        Order: **fresh listener-request songs first** (made in the last
        `REQUEST_PRIORITY_H` hours, newest first — they air minutes after
        being made, so a cover rendered now lands before the first spin;
        ALBUM_ART Step 5 addendum), then songs already in the committed
        program (they are about to air), then newest `created_at`.
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.* FROM items i
                   WHERE i.type='song' AND i.retired=0 AND i.emergency=0
                     AND i.art_path IS NULL
                     AND COALESCE(i.title,'') <> '' AND COALESCE(i.artist,'') <> ''
                     AND (i.art_attempted_at IS NULL OR i.art_attempted_at <= ?)
                   ORDER BY CASE WHEN i.meta_json LIKE '%"request_id"%'
                                  AND i.created_at >= ?
                            THEN 0 ELSE 1 END,
                            CASE WHEN EXISTS (
                              SELECT 1 FROM program p WHERE p.item_id = i.id)
                            THEN 0 ELSE 1 END,
                            COALESCE((SELECT MIN(p.seq) FROM program p
                                      WHERE p.item_id = i.id), 0),
                            i.created_at DESC
                   LIMIT ?""",
                (now - retry_after_s, _iso_ago(now, REQUEST_PRIORITY_H), limit)).fetchall()
        return [dict(r) for r in rows]

    def set_art(self, item_id: int, art_path: str, art_meta: dict[str, Any]) -> None:
        """Record a rendered cover: the local JPEG plus the reproducible scene
        (scene, final FLUX prompt, seed) merged into the item's meta."""
        with self._lock:
            r = self._conn.execute(
                "SELECT meta_json FROM items WHERE id=?", (item_id,)).fetchone()
            meta = json.loads(r["meta_json"]) if r and r["meta_json"] else {}
            meta["art"] = art_meta
            # A re-render (art_backfill --force) writes new bytes, so any
            # previous upload no longer matches: clear the key and let the
            # publisher upload the new JPEG (same rule as set_media_file).
            self._conn.execute(
                "UPDATE items SET art_path=?, meta_json=?, art_attempted_at=NULL, "
                "art_s3_key=NULL, art_uploaded_at=NULL WHERE id=?",
                (art_path, json.dumps(meta), item_id))
            self._items_changed()
            self._conn.commit()

    def set_art_attempted(self, item_id: int, when: float) -> None:
        """Mark a failed art attempt (back-off for `art.retry_after_s`)."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET art_attempted_at=? WHERE id=?", (when, item_id))
            self._items_changed()
            self._conn.commit()

    def set_art_published(self, item_id: int, art_s3_key: str) -> None:
        """Record a confirmed art upload. Only a successful PUT calls this."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET art_s3_key=?, art_uploaded_at=? WHERE id=?",
                (art_s3_key, _now_iso(), item_id))
            self._items_changed()
            self._conn.commit()

    def items_to_publish_art(self, limit: int = 50) -> list[dict[str, Any]]:
        """Songs with a rendered JPEG not yet in S3 (docs/shipped/album-art.md Step 6)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE type='song' AND retired=0 "
                "AND art_path IS NOT NULL AND art_s3_key IS NULL "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def art_counts(self) -> dict[str, int]:
        """Songs with / without a rendered cover (admin health only)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT SUM(CASE WHEN art_path IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN art_path IS NULL THEN 1 ELSE 0 END) FROM items "
                "WHERE type='song' AND retired=0 AND emergency=0").fetchone()
        return {"with_art": int(r[0] or 0), "without_art": int(r[1] or 0)}

    def set_media_file(self, item_id: int, *, media_path: str, duration_s: float,
                       sample_rate: int, channels: int) -> None:
        """Point an item at a re-encoded file (tools/mp3_migrate). The old file
        stays on disk (hard rule 5); any previous upload no longer matches, so
        s3_key is cleared and the uploader re-publishes."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET media_path=?, duration_s=?, sample_rate=?, "
                "channels=?, s3_key=NULL, uploaded_at=NULL WHERE id=?",
                (media_path, duration_s, sample_rate, channels, item_id))
            self._items_changed()
            self._conn.commit()

    def _items_changed(self) -> None:
        """Call after any write to `items` (under the lock)."""
        self._item_cache.clear()

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        now = _cache_clock()
        with self._lock:
            hit = self._item_cache.get(int(item_id))
            if hit is not None and now - hit[1] < ITEM_CACHE_TTL_S:
                return dict(hit[0])
            r = self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if r is None:
                self._item_cache.pop(int(item_id), None)
                return None
            row = dict(r)
            if len(self._item_cache) >= ITEM_CACHE_MAX:
                self._item_cache.clear()
            self._item_cache[int(item_id)] = (row, now)
        return dict(row)

    def list_items(self, type_: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE retired=0"
        args: tuple = ()
        if type_:
            q += " AND type=?"
            args = (type_,)
        q += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def count_fresh_of_type(self, type_: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND fresh=1 AND emergency=0 "
                "AND retired=0",
                (type_,)).fetchone()[0])

    def count_fresh_unexpired_of_type(self, type_: str, now_wall: float) -> int:
        """Unaired, non-emergency items still valid at `now_wall`.

        Contextual types (dj_talk, field_report, news) deplete: an expired clip
        is not stock, so the selector and /api/health must agree with
        `Producer._count_fresh_unexpired` (RADIO §5.2, OVERHAUL 5.3).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT expires_at FROM items WHERE type=? AND fresh=1 "
                "AND emergency=0 AND retired=0", (type_,)).fetchall()
        n = 0
        for r in rows:
            exp = _parse_epoch(r["expires_at"])
            if exp is not None and exp <= now_wall:
                continue
            n += 1
        return n

    def count_usable_of_type(self, type_: str) -> int:
        """Evergreen types recycle: any non-emergency item is usable whatever its
        fresh flag (RADIO.md §5.4 fallback chain).
        """
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM items WHERE type=? AND emergency=0 AND retired=0",
                (type_,)).fetchone()[0])

    def retire_item(self, item_id: int, reason: str) -> None:
        """Flag an item retired (excluded from playout + production) with the
        reason merged into its meta. Rows stay for history; files stay on disk.
        """
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                return
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            meta["retired_reason"] = reason
            self._conn.execute(
                "UPDATE items SET retired=1, meta_json=? WHERE id=?",
                (json.dumps(meta), item_id))
            if r["type"] == "song":
                self._bump_catalogue()
            self._items_changed()
            self._conn.commit()

    def update_item_meta(self, item_id: int, meta: dict) -> None:
        """Replace an item's meta (e.g. SFX overlays attached after storage)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT type FROM items WHERE id=?", (item_id,)).fetchone()
            self._conn.execute("UPDATE items SET meta_json=? WHERE id=?",
                               (json.dumps(meta) if meta else None, item_id))
            # catalogue membership (a ``song`` set member) can change via meta in
            # future edit paths; keep the console's revision honest about the set
            if r and r["type"] == "song":
                self._bump_catalogue()
            self._items_changed()
            self._conn.commit()

    # ------------------------------------------------------- DJ catalogue (11)
    def _bump_catalogue(self) -> None:
        """Increment catalogue_revision (under the store lock). Bumped only when
        the **song** catalogue set changes (add/retire/edit), so the console can
        detect a stale page and refetch (docs/systems/console.md §4)."""
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('catalogue_revision','1') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(CAST(meta.value AS INTEGER) + 1 AS TEXT)")

    def catalogue_revision(self) -> int:
        with self._lock:
            return self._catalogue_revision_locked()

    def _catalogue_revision_locked(self) -> int:
        r = self._conn.execute(
            "SELECT value FROM meta WHERE key='catalogue_revision'").fetchone()
        return int(r[0]) if r and r[0] else 0

    def song_catalogue(self, q: str | None, sort: str, direction: str,
                       cursor: tuple[str, str, int] | None,
                       limit: int) -> tuple[list[dict[str, Any]],
                                            tuple[str, str, int] | None, int]:
        """All non-retired, non-emergency library songs (fresh AND already
        aired; docs/systems/console.md §2). `sort` is allowlisted title|artist; the tie-break is
        the other column then id, so sorting never reorders or duplicates rows
        across pages. The cursor is (sort_value, other_value, id) of the last
        row returned. Returns (rows, next_cursor, catalogue_revision); fetches
        one extra row to know whether more remain. The only interpolated SQL
        is the allowlisted column/direction pair (AGENTS §5)."""
        if sort not in ("title", "artist") or direction not in ("asc", "desc"):
            raise ValueError("invalid sort/direction")
        order = "ASC" if direction == "asc" else "DESC"
        other = "artist" if sort == "title" else "title"
        where = ["type='song'", "retired=0", "emergency=0"]
        args: list[Any] = []
        # Case-insensitive via LOWER() on both sides (portable; SQLite's
        # COLLATE NOCASE has no Postgres equivalent and both are ASCII-only).
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{esc.lower()}%"
            where.append("(LOWER(COALESCE(title,'')) LIKE ? ESCAPE '\\' OR "
                         "LOWER(COALESCE(artist,'')) LIKE ? ESCAPE '\\')")
            args += [like, like]
        lsort = f"LOWER(COALESCE({sort},''))"
        lother = f"LOWER(COALESCE({other},''))"
        if cursor:
            p0, s0, i0 = cursor
            p0, s0 = p0.lower(), s0.lower()
            op = ">" if direction == "asc" else "<"
            where.append(
                f"({lsort} {op} ? OR ({lsort} = ? AND "
                f"({lother} {op} ? OR ({lother} = ? "
                f"AND id {op} ?))))")
            args += [p0, p0, s0, s0, i0]
        sql = (f"SELECT id, title, artist, duration_s, fresh, last_aired_at "
               f"FROM items WHERE {' AND '.join(where)} "
               f"ORDER BY {lsort} {order}, "
               f"{lother} {order}, id {order} LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, args + [limit + 1]).fetchall()
            rev = self._catalogue_revision_locked()
        out = [dict(r) for r in rows[:limit]]
        nxt = None
        if len(rows) > limit:
            last = rows[limit - 1]
            nxt = (str(last[sort] or ""), str(last[other] or ""), int(last["id"]))
        return out, nxt, rev

    def mark_aired(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE items SET fresh=0 WHERE id=?", (item_id,))
            self._items_changed()
            self._conn.commit()

    def commit_song_air(self, item_id: int, wall: float) -> None:
        """Persist a song's air at commit time (PRIORITIES §4). `wall` is the
        injected clock's wall epoch (never time.time()). Spacing and the
        recycled-song weighting read last_aired_at, so a restart keeps the
        rotation instead of resetting it. play_count feeds the stats endpoint."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET last_aired_at=?, play_count=play_count+1 WHERE id=?",
                (wall, item_id))
            self._items_changed()
            self._conn.commit()

    def backfill_airplay_history(self) -> None:
        """One-time backfill (PRIORITIES §4): for items that predate the
        last_aired_at/play_count columns, seed them from the airplay ledger so
        the live pool starts with real history rather than all-NULL. Rerunnable
        and safe: only NULL last_aired_at rows are touched, so live values set
        by read/replay of a given song survive."""
        with self._lock:
            # Only the rows that still need it: on Postgres over the tailnet
            # every statement is a round trip, and a blanket UPDATE per item
            # (6.5k of them) held the station's start for minutes (2026-11-05).
            need = [int(r[0]) for r in self._conn.execute(
                "SELECT id FROM items WHERE last_aired_at IS NULL "
                "AND id IN (SELECT item_id FROM airplay)").fetchall()]
            if not need:
                return
            marks = ",".join("?" * len(need))
            rows = self._conn.execute(
                "SELECT item_id, COUNT(*) AS plays, MAX(recorded_at) AS last "
                f"FROM airplay WHERE item_id IN ({marks}) GROUP BY item_id",
                tuple(need)).fetchall()
            for r in rows:
                last = _parse_epoch(r["last"])
                self._conn.execute(
                    "UPDATE items SET last_aired_at=?, play_count=? "
                    "WHERE id=? AND last_aired_at IS NULL",
                    (last, r["plays"], r["item_id"]))
            self._items_changed()
            self._conn.commit()

    def last_played_at(self, item_id: int) -> float | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT MAX(recorded_at) FROM airplay WHERE item_id=?", (item_id,)).fetchone()
        return r[0]

    def play_count(self, item_id: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM airplay WHERE item_id=?", (item_id,)).fetchone()
        return int(r[0])
