"""Manage paid-request promo codes (2026-11-08; RADIO §11, §14).

    python -m pilgrim.tools.promo_codes add --code RIDE-FREE --uses 5
    python -m pilgrim.tools.promo_codes list
    python -m pilgrim.tools.promo_codes redemptions --code RIDE-FREE
    python -m pilgrim.tools.promo_codes retire --code RIDE-FREE
    python -m pilgrim.tools.promo_codes activate --code RIDE-FREE

Uses the same store as the station: `DATABASE_URL` when set (production
Postgres), else `library.db` under the package root. Codes are stored
upper-case; each code has a fixed number of uses and one redemption per
browser identity. An exhausted code is retired (active=0) but never deleted,
so a guessed spent code answers "all used up" rather than "invalid".
"""
from __future__ import annotations

import argparse
import sys

from pilgrim.config import load_config
from pilgrim.db import open_store


def _print_code(row: dict) -> None:
    state = "active" if int(row.get("active") or 0) else (
        row.get("retired_by") or "retired")
    print(f"{row['code']:<20} {int(row['uses']):>4}/{int(row['max_uses']):<4} "
          f"{state:<10} {row.get('note') or ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="create a code with N uses")
    add.add_argument("--code", required=True)
    add.add_argument("--uses", type=int, required=True)
    add.add_argument("--note", default=None)

    sub.add_parser("list", help="show every code, active and retired")

    red = sub.add_parser("redemptions", help="show who used a code")
    red.add_argument("--code", required=True)

    retire = sub.add_parser("retire", help="remove a code from circulation")
    retire.add_argument("--code", required=True)

    activate = sub.add_parser("activate", help="put a retired code back (if uses remain)")
    activate.add_argument("--code", required=True)

    args = parser.parse_args(argv)
    cfg = load_config()
    store = open_store(cfg)
    try:
        if args.cmd == "add":
            try:
                row = store.create_promo_code(args.code, args.uses, args.note)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            except store.IntegrityError:
                print(f"{args.code.strip().upper()} already exists", file=sys.stderr)
                return 1
            print("created:")
            _print_code(row)
        elif args.cmd == "list":
            rows = store.list_promo_codes()
            if not rows:
                print("(no promo codes)")
                return 0
            print(f"{'CODE':<20} {'USES':<9} {'STATE':<10} NOTE")
            for row in rows:
                _print_code(row)
        elif args.cmd == "redemptions":
            rows = store.promo_redemptions(args.code)
            if not rows:
                print("(no redemptions)")
                return 0
            for row in rows:
                print(f"{row['identity']:<40} {row['created_at']} "
                      f"request={row.get('request_id') or '—'}")
        elif args.cmd == "retire":
            if not store.retire_promo_code(args.code):
                print("no such code", file=sys.stderr)
                return 1
            print(f"{args.code.strip().upper()} retired")
        elif args.cmd == "activate":
            if not store.activate_promo_code(args.code):
                print("no such code, or no uses remain", file=sys.stderr)
                return 1
            print(f"{args.code.strip().upper()} active")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
