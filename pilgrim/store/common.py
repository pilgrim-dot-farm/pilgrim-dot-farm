"""SQL fragments and row projections shared by the store modules (RADIO §14).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pilgrim.pipelines.request_parts import RequestParts


# Queue order everywhere (RADIO §14; supersedes docs/shipped/payments.md §3.2).
#
#   1. paid first (priority DESC) — paid priority is never voted on
#   2. work already in flight (producing/ready) before free queued rows, so a
#      job that started is never reordered
#   3. free queued rows by vote score DESC (votes decide what is written next)
#   4. then paid_at / claimed_at / created_at ASC, id ASC — the old FIFO, so
#      with no votes nothing changes.
#
# The score is read from request_votes on every query (docs/shipped/voting.md V9): always
# accurate, no cached counters to keep in sync.
def queue_order(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    score = ("(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
             f"WHERE v.request_id = {a}id)")
    return (
        f"ORDER BY {a}priority DESC, "
        f"CASE WHEN {a}status = 'queued' THEN 1 ELSE 0 END ASC, "
        f"CASE WHEN {a}status = 'queued' AND {a}priority = 0 "
        f"THEN -{score} ELSE 0 END ASC, "
        f"COALESCE({a}paid_at, {a}claimed_at, {a}created_at) ASC, "
        f"{a}id ASC")


QUEUE_ORDER = queue_order()
# The same order for queries that alias requests as `r` (joined with items).
QUEUE_ORDER_R = queue_order("r")


# Benchmark rows (docs/systems/queue-bench.md) are left out of everything a
# listener can see or that counts: the board, the hit counter, the free cap
# and the scheduler's hold. The producer's own queue queries keep them in.
NOT_BENCH = "COALESCE(bench, 0)=0"


def _part_values(parts: RequestParts | None) -> tuple[str | None, ...]:
    """The three paid-request columns: NULL for a legacy row, `""` for a box
    the listener left empty (docs/shipped/prompt-upgrade.md Step 2)."""
    if parts is None:
        return (None, None, None)
    return (parts.lyrics, parts.music, parts.art)


def _queued_where(bury_threshold: int | None) -> tuple[str, tuple[Any, ...]]:
    """WHERE clause for the air-able queue: `queued` rows, minus buried free
    ones when a threshold is given (docs/shipped/voting.md V7)."""
    where = "status='queued'"
    params: tuple[Any, ...] = ()
    if bury_threshold is not None:
        where += (" AND NOT (priority = 0 AND "
                  "(SELECT COALESCE(SUM(v.value), 0) FROM request_votes v "
                  "WHERE v.request_id = requests.id) <= ?)")
        params = (int(bury_threshold),)
    return where, params



def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _iso_at(at: float | None = None) -> str:
    """ISO timestamp for `at` (epoch seconds), else now."""
    return ((datetime.fromtimestamp(at, UTC) if at is not None
             else datetime.now(UTC)).isoformat())


def _promo_norm(code: str) -> str:
    """Promo codes are case-insensitive, trimmed and stored upper-case."""
    return (code or "").strip().upper()


def _parse_epoch(iso: str | None) -> float | None:
    """Parse an ISO-8601 timestamp into a wall-clock epoch (PRIORITIES §4)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def _project_text(text: str, paid: bool, max_length: int,
                  summary: str | None = None) -> str:
    """Public board text. Paid rows show the generated summary when present,
    else the first line, collapsed and cut to the request-line limit
    (docs/shipped/payments.md §3.4); free rows are unchanged."""
    if not paid:
        return text
    if summary:
        return summary[:max_length]
    first = " ".join(text.split("\n", 1)[0].split())
    if len(first) > max_length:
        first = first[: max_length - 1] + "…"
    return first


def _wait_s(prep_started_at: str | None, aired_at: str | None,
            due_at: str | None = None) -> int | None:
    """Prep time on the board: the prep clock starts when the lyrics/brief
    are first written (`prep_started_at`), NOT at submission — time spent in
    line or waiting for the station to come back is not prep work. Rows that
    never recorded a writing start (aired before this column existed) read
    null, so the UI omits the label rather than showing queue idle time.

    The clock stops at the earlier of `aired_at` and `due_at` (RADIO §11):
    `due_at` is the wall time the song was scheduled to air when it was teed
    up (committed or spliced). With listeners present the two agree to within
    seconds; with nobody listening the playhead is frozen (docs/shipped/idle.md D3) and
    `aired_at` only lands on the next PLAY, hours later — that wait is nobody's
    prep, so the metric is capped at when the song WOULD have aired."""
    start = _parse_epoch(prep_started_at)
    ends = [t for t in (_parse_epoch(aired_at), _parse_epoch(due_at)) if t is not None]
    if start is None or not ends:
        return None
    return max(0, int(round(min(ends) - start)))


def _project_queue_row(row: dict[str, Any], max_length: int,
                       stages: Mapping[int, str] | None = None,
                       score: int = 0, votable: bool = False,
                       buried: bool = False) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    stage = ""
    if row["status"] == "producing":
        # writing = lyrics/brief, generating = song model (RADIO §11)
        stage = (stages or {}).get(int(row["id"]), "writing")
    return {"id": int(row["id"]),
            "text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "status": row["status"], "paid": paid, "stage": stage,
            # the song exists from `ready` on (title shown in the board table)
            "song_title": row.get("song_title") or "",
            # docs/shipped/voting.md: score is display-only once the row is in flight
            "score": int(score), "votable": bool(votable),
            "buried": bool(buried)}


def _project_recent_row(row: dict[str, Any], max_length: int) -> dict[str, Any]:
    paid = bool(row.get("priority"))
    return {"text": _project_text(row["text"], paid, max_length,
                                  row.get("summary")),
            "song_title": row.get("song_title") or "",
            "song_artist": row.get("song_artist") or "",
            "paid": paid,
            "wait_s": _wait_s(row.get("prep_started_at"), row.get("aired_at"),
                              row.get("due_at"))}


REQUEST_PRIORITY_H = 24.0   # a request song made this recently jumps the queue




def _iso_ago(epoch: float, hours: float) -> str:
    return datetime.fromtimestamp(epoch - hours * 3600, tz=UTC).isoformat()
