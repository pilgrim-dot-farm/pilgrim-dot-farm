"""station.db: inventory metadata + airplay ledger + committed program (RADIO.md §7, §11).

Engine-neutral since docs/pending/scalability.md Step 2: every statement is written once in
SQLite's flavour and runs unchanged on SQLite (tests, sim, dev) or Postgres
(production) through `pilgrim.db`. Keep it that way: `?` placeholders, INTEGER
flags, TEXT ISO timestamps, REAL epochs, `RETURNING` for new ids, upserts as
`ON CONFLICT`, no `COLLATE NOCASE` (use LOWER), no PRAGMA/sqlite_sequence
outside `pilgrim.db`.

The store is one class, `Store`, assembled from one module per group of
tables. Engine differences live only in `pilgrim.db`; never branch on the
engine in this package.
"""
from __future__ import annotations

from pilgrim.store.chat import ChatStore
from pilgrim.store.common import _parse_epoch
from pilgrim.store.items import ItemsStore
from pilgrim.store.paid import PaidStore
from pilgrim.store.program import ProgramStore
from pilgrim.store.promo import PromoStore
from pilgrim.store.votes import VotesStore

__all__ = ["Store", "_parse_epoch"]


class Store(ItemsStore, ProgramStore, PaidStore, VotesStore, PromoStore, ChatStore):
    """Thin wrapper over one database connection (SQLite file or Postgres DSN).
    All access goes through `_lock`; writes commit explicitly."""
