"""Obscured prohibited terms and reserved display names (docs/plans/sheep-pen-brief.md §0.6.2).

The repo never contains a plaintext prohibited term or reserved-name literal.
Entries are SHA-256 hex digests of the *normalized* form (NFKC → casefold →
whitespace-collapsed). Matching hashes a candidate token / bigram / trigram and
looks it up in the set — never a substring search.

Evasions are matched without ever knowing the terms: a candidate is rewritten
into the spellings it could stand for (digit/symbol look-alikes, a stand-in
character for any letter, homoglyphs, accents, stretched letters, separators,
spaced-out letters, number padding) and each spelling is hashed and looked up.
Messages match whole words only, so an innocent word that merely contains a
term passes; display names are locked and shown on every message, so they also
match a term embedded anywhere in the name (`embedded=True`).

Accepted limitation (docs/plans/sheep-pen-brief.md §0.6.2): creative respellings
(a dropped letter, a phonetic spelling, a term glued to another word in a
message) still fall to Layer B, and a dictionary attack can recover the list.
The goal is no plaintext in the repo and logs, not secrecy.

Operator step: feed terms on stdin to `pilgrim/tools/add_prohibited_term.py`;
it prints digests only. Paste them here. Never commit, log or echo a term.
"""
from __future__ import annotations

import hashlib
import itertools
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

# Station characters and operator names (docs/plans/sheep-pen-brief.md §0.6.1): Giles, the DJ,
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


# --- evasion matching -------------------------------------------------------
# Character tables only: nothing below names or implies a term.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
# Digits and symbols that stand in for a letter.
_LOOKALIKE: dict[str, str] = {
    "0": "o", "1": "il", "2": "z", "3": "e", "4": "a", "5": "s", "6": "gb",
    "7": "t", "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "il",
    "+": "t", "€": "e", "£": "l", "¡": "i",
}
# Letters commonly swapped for another letter.
_LETTER_ALT: dict[str, str] = {"l": "i", "q": "g", "z": "s", "v": "u"}
# Cyrillic and Greek letters that render like a Latin one.
_HOMOGLYPH = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j",
    "ѕ": "s", "һ": "h", "ԁ": "d", "ɡ": "g", "ı": "i",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
})
_SPELLING_CAP = 1500     # spellings tried per unit and pass
_HASH_BUDGET = 30000     # digests per screened text; past it Layer B decides
_EMBED_MIN, _EMBED_MAX = 4, 20   # embedded-term lengths tried in a name
_JOIN_MAX = 12           # chunks joined when a term is spaced out


def _fold(text: str) -> str:
    """normalize() plus accent stripping and homoglyph folding."""
    decomposed = unicodedata.normalize("NFKD", normalize(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.translate(_HOMOGLYPH)


def _units(folded: str, embedded: bool) -> list[str]:
    """Whitespace chunks, plus the joins that undo a spaced-out term: a run of
    chunks where at most one is longer than a character, or three or more
    chunks of at most two characters. Names also try the whole name joined."""
    chunks = folded.split()
    units = list(chunks)
    for i in range(len(chunks)):
        for j in range(i + 2, min(len(chunks), i + _JOIN_MAX) + 1):
            window = chunks[i:j]
            long_ones = sum(1 for c in window if len(c) > 1)
            if long_ones <= 1 or (len(window) >= 3 and all(len(c) <= 2 for c in window)):
                units.append("".join(window))
    if embedded and len(chunks) > 1:
        units.append("".join(chunks))
    return list(dict.fromkeys(units))


def _options(unit: str, *, alts: bool, wild: bool) -> list[tuple[str, ...]]:
    """Per character, the letters it may stand for ("" = may be dropped)."""
    letters = [ch.isalpha() for ch in unit]
    if True not in letters:
        return []
    first = letters.index(True)
    last = len(letters) - 1 - letters[::-1].index(True)
    inner = [i for i in range(first, last + 1) if not letters[i]]
    wild = wild and 1 <= len(inner) <= 2 and sum(letters) >= 3
    out: list[tuple[str, ...]] = []
    for i, ch in enumerate(unit):
        if letters[i]:
            out.append((ch, *_LETTER_ALT.get(ch, "")) if alts else (ch,))
            continue
        look = tuple(_LOOKALIKE.get(ch, ""))
        if first < i < last:
            if wild:
                out.append(tuple(_ALPHABET))        # a stand-in for any letter
            elif ch.isdigit():
                out.append(look or ("",))
            else:
                out.append((*look, ""))             # symbol or separator
        else:
            out.append((*look, ""))                 # padding around the word
    return out


def _spellings(unit: str, *, wild: bool) -> list[str]:
    for alts in (True, False):
        opts = _options(unit, alts=alts, wild=wild)
        size = 1
        for o in opts:
            size *= len(o)
        if opts and size <= _SPELLING_CAP:
            return ["".join(p) for p in itertools.product(*opts)]
    return []


def _squeezed(word: str) -> set[str]:
    """`word` with stretched letters squeezed: each repeated run kept at one
    or two characters (all combinations while there are few runs)."""
    runs = [(k, len(list(g))) for k, g in itertools.groupby(word)]
    repeated = [n for _, n in runs if n > 1]
    if not repeated:
        return {word}
    out = {word}
    if len(repeated) <= 4:
        choices = [(k,) if n == 1 else (k, k * 2) for k, n in runs]
        out.update("".join(p) for p in itertools.product(*choices))
    else:
        out.add("".join(k for k, _ in runs))
        out.add("".join(k * min(n, 2) for k, n in runs))
    return out


def _evasion_hit(text: str, digests: frozenset[str], embedded: bool) -> bool:
    seen: set[str] = set()
    budget = _HASH_BUDGET

    def check(candidate: str) -> bool:
        nonlocal budget
        if len(candidate) < 2 or candidate in seen or budget <= 0:
            return False
        seen.add(candidate)
        budget -= 1
        return hashlib.sha256(candidate.encode("utf-8")).hexdigest() in digests

    units = _units(_fold(text), embedded)
    for wild in (False, True):            # cheap spellings first, stand-ins last
        for unit in units:
            for spelling in _spellings(unit, wild=wild):
                for word in _squeezed(spelling):
                    if check(word) or (word[-1] in "sz" and check(word[:-1])):
                        return True
                    if not embedded:
                        continue
                    for n in range(_EMBED_MIN, min(len(word), _EMBED_MAX) + 1):
                        for i in range(len(word) - n + 1):
                            if check(word[i:i + n]):
                                return True
                if budget <= 0:
                    return False
    return False


def prohibited_hit(text: str, *, embedded: bool = False) -> bool:
    """True when `text` carries a term on the digest list.

    Exact tokens, bigrams and trigrams first, then the evasion spellings (see
    the module docstring). `embedded=True` (display names) also matches a term
    inside a longer word.
    """
    words = tokens(text)
    candidates = list(words)
    candidates += [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    candidates += [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    if any(digest(c) in PROHIBITED_DIGESTS for c in candidates):
        return True
    return _evasion_hit(text, PROHIBITED_DIGESTS, embedded)


def is_reserved_name(display: str) -> bool:
    """Reserved display names match on the whole normalized name only."""
    return digest(display) in RESERVED_NAME_DIGESTS
