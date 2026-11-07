"""Obscured prohibited terms and reserved display names (SHEEP_PEN.md §0.6.2).

The repo never contains a plaintext prohibited term or reserved-name literal.
Entries are SHA-256 hex digests of the *normalized* form (NFKC → casefold →
whitespace-collapsed). Matching hashes a candidate token / bigram / trigram and
looks it up in the set — never a substring search.

Accepted limitation (SHEEP_PEN.md §0.6.2): matching is token-exact, so
leetspeak and homoglyph evasions fall to Layer B, and a dictionary attack can
recover the list. The goal is no plaintext in the repo and logs, not secrecy.

Operator step: feed terms on stdin to `pilgrim/tools/add_prohibited_term.py`;
it prints digests only. Paste them here. Never commit, log or echo a term.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# Extreme slurs / abuse terms, digest-only (tokens and multi-word phrases).
PROHIBITED_DIGESTS: frozenset[str] = frozenset({
    "120f6e5b4ea32f65bda68452fcfaaef06b0136e1d0e4a6f60bc3771fa0936dd6",
    "5b3ae48be122f7ed19b4cc587649f41f9d2565df51cfa332f8e7806f4ebb9032",
    "8f5083e3e5c7dc8932f2bf58212f963f3a44752618c96297f82623f736c52738",
    "1e02eec4f1095143be282056557034d4e8ab915342c1af508223141d472d2347",
    "c3de533e9b7fe63b79f648687a30d2861edd92fe7c3cd1f2c485e0a605367624",
    "16ea09fc78ca83ca502cbcf2377acdf280bf18f61e259153f0868405eedab5ef",
    "158869a97379229b7681efae9d7f9c9214134e836d649ba53477c0c111414d59",
    "c1cabb6f6e431f9c4dea4c3b3264d6fe3829241d674f3496a2dfff6658f0363e",
    "f9d0d9b18ae9033a5ea36df19bf279b059e887a9ae785db81117bceaecc95933",
    "98b52c4b6b7d1f48e7477a5ccc10955dd195d0ac5a38c8281bfeb08762634909",
    "eef3bd091670c3447022d619c06ad15de96da72b5a66f28bb8b75d1b1c12a05f",
    "e7b98c6aa5b944e0b315d350d423f895ac9e44fb84f1534b18c2572370a67b9e",
    "cc02032349c833ac5e97bac094560ed40e09acf34cb1978ab7a9840b9bf15b4d",
})

# Station characters and operator names (SHEEP_PEN.md §0.6.1): Giles, the DJ,
# operator/admin/moderator/system. Matched on the whole normalized display name.
RESERVED_NAME_DIGESTS: frozenset[str] = frozenset({
    "a0b0f4a525b340639c00b5275ddfe98a792bb6d6b5886c1a10f31259e935ed43",
    "e3188c049022dfb663b5b4c9e4557a27e7872835ee3302d17ca8b85640ade6cb",
    "06e55b633481f7bb072957eabcf110c972e86691c3cfedabe088024bffe42f23",
    "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918",
    "cfde2ca5188afb7bdd0691c7bef887baba78b709aadde8e8c535329d5751e6fe",
    "bbc5e661e106c6dcd8dc6dd186454c2fcba3c710fb4d8e71a60c93eaf077f073",
    "1dd31cd072fdd30f9bbb81f6d4d3eeacfef383028f6f8bb1376840f07ef6d370",
    "9b4ef3e5b7b4d1759cb8f880e34dbd5ea8c3fcd79ce33b89d7357bf64fcbfc91",
})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)   # unicode alphanumeric runs

# A digest entry that is not a digest is a plaintext term smuggled into the
# repo: fail the import instead of shipping it.
for _digest in PROHIBITED_DIGESTS | RESERVED_NAME_DIGESTS:
    assert _HEX64.fullmatch(_digest), "prohibited lists must be SHA-256 hex digests"


def normalize(text: str) -> str:
    """NFKC → casefold → collapse whitespace → strip."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def tokens(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def prohibited_hit(text: str) -> bool:
    """True when a token, bigram or trigram of `text` is on the digest list."""
    words = tokens(text)
    candidates = list(words)
    candidates += [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    candidates += [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    return any(digest(c) in PROHIBITED_DIGESTS for c in candidates)


def is_reserved_name(display: str) -> bool:
    """Reserved display names match on the whole normalized name only."""
    return digest(display) in RESERVED_NAME_DIGESTS
