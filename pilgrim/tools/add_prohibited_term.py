"""Operator tool: print SHA-256 digests for prohibited terms / reserved names.

SHEEP_PEN.md §0.6.2. Terms arrive on stdin, one per line. The tool prints only
digests; it never writes a term to a file, a log, or an error message, and it
does not echo the input. Paste the printed digests into
`pilgrim/chat/prohibited.py`.

    python -m pilgrim.tools.add_prohibited_term
    python -m pilgrim.tools.add_prohibited_term --reserved

Never commit a term. Never paste one into a commit message, a report or a log.
"""
from __future__ import annotations

import sys

from pilgrim.chat.prohibited import digest, normalize


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    reserved = "--reserved" in args
    label = "RESERVED_NAME_DIGESTS" if reserved else "PROHIBITED_DIGESTS"
    print(f"# {label}")
    for line in sys.stdin:
        term = normalize(line)
        if not term:
            continue
        print(f'    "{digest(term)}",')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
