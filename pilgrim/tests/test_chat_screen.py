"""Obscured prohibited terms, reserved names and Layer A screening."""
from __future__ import annotations

import hashlib
import re

from pilgrim.chat import prohibited
from pilgrim.chat.screen import screen_text

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_every_entry_is_a_digest_and_no_plaintext():
    for entry in prohibited.PROHIBITED_DIGESTS | prohibited.RESERVED_NAME_DIGESTS:
        assert HEX64.fullmatch(entry)


def test_digest_list_hit_rejects(monkeypatch):
    phrase = "barnyard menace"
    monkeypatch.setattr(prohibited, "PROHIBITED_DIGESTS",
                        frozenset({hashlib.sha256(
                            prohibited.normalize(phrase).encode()).hexdigest()}))
    assert prohibited.prohibited_hit("that barnyard menace again")
    assert not prohibited.prohibited_hit("barnyard friends")


def _only(monkeypatch, *terms):
    monkeypatch.setattr(prohibited, "PROHIBITED_DIGESTS", frozenset(
        hashlib.sha256(prohibited.normalize(t).encode()).hexdigest() for t in terms))


def test_evasion_spellings_are_caught(monkeypatch, cfg):
    # A harmless stand-in term: the real list is digests only.
    _only(monkeypatch, "tractor")
    for text in ("tr4ct0r", "TRACTOR", "tra33or", "tr*ctor", "tr@ctor",
                 "t.r.a.c.t.o.r", "t r a c t o r", "tr ac to r", "t ractor",
                 "traaaactorrr", "tractor99", "420tractor", "tractors",
                 "tractorz", "tr\u00e4ctor", "tr\u0430ct\u043er", "you old tr4ct0r!"):
        assert prohibited.prohibited_hit(text), text
        assert screen_text(text, cfg.chat).code == "prohibited", text


def test_evasion_matching_spares_innocent_words(monkeypatch, cfg):
    _only(monkeypatch, "tractor", "heck")
    for text in ("classic tractive effort", "contractor", "subtractors",
                 "trac tor", "the check is in", "checkers", "he ck", "h3 said ok",
                 "it's a l33t d4y, don't y'all th1nk?!", "tr??or", "t1r2a3c4t5o6r"):
        assert not prohibited.prohibited_hit(text), text
        assert screen_text(text, cfg.chat).ok, text
    # reserved names stay whole-name exact
    assert not prohibited.prohibited_hit("g1les")
    assert screen_text("g1les was here", cfg.chat).ok


def test_names_also_match_an_embedded_term(monkeypatch, cfg):
    from pilgrim.chat.identity import validate_name

    _only(monkeypatch, "tractor")
    for name in ("tractor", "tra33or", "xXtractorXx", "xXtr4ct0rXx", "Big Trac Tor",
                 "tractorfan99", "T R A C T O R"):
        assert validate_name(name, cfg.chat) == "name_invalid", name
    for name in ("Tracy", "Old Tracker", "Farmer Ada"):
        assert validate_name(name, cfg.chat) is None, name


def test_evasion_matching_is_bounded(monkeypatch):
    _only(monkeypatch, "tractor")
    calls = 0
    real = hashlib.sha256

    def counting(data=b""):
        nonlocal calls
        calls += 1
        return real(data)

    monkeypatch.setattr(prohibited.hashlib, "sha256", counting)
    assert not prohibited.prohibited_hit(" ".join(f"a{i}b{i}c" for i in range(40)))
    assert calls <= prohibited._HASH_BUDGET + 200


def test_reserved_names_are_digest_matched():
    assert prohibited.is_reserved_name("GILES")
    assert prohibited.is_reserved_name("  giles ")
    assert prohibited.is_reserved_name("Operator")
    assert not prohibited.is_reserved_name("gil es")


def test_no_profanity_filtering(cfg):
    for text in ("shit happens", "fucking tractors", "damn"):
        res = screen_text(text, cfg.chat)
        assert res.ok, text
        assert res.text == text
        assert "profanity" not in res.flags


def test_empty_and_trim(cfg):
    assert not screen_text("   ", cfg.chat).ok
    assert screen_text("  hello  ", cfg.chat).text == "hello"


def test_length_is_code_points(cfg):
    cfg.chat.max_length = 5
    assert screen_text("12345", cfg.chat).ok
    assert not screen_text("123456", cfg.chat).ok
    # an emoji is one code point, not four UTF-16 units
    assert screen_text("🐑🐑🐑🐑🐑", cfg.chat).ok


def test_links_are_rejected(cfg):
    for text in ("https://example.com", "www.example.com", "example.com",
                 "me at a dot com", "a@b.co", "call 5551234567", "@handle"):
        res = screen_text(text, cfg.chat)
        assert not res.ok, text
        assert res.code == "link", text


def test_html_and_control_characters_rejected(cfg):
    assert not screen_text("<script>alert(1)</script>", cfg.chat).ok
    assert screen_text("<script>alert(1)</script>", cfg.chat).code == "bad_chars"
    assert not screen_text("hello\x00world", cfg.chat).ok
    assert not screen_text("zero\u200bwidth", cfg.chat).ok


def test_repeat_rejected(cfg):
    res = screen_text("same thing", cfg.chat, repeated=True)
    assert not res.ok and res.code == "repeat"


def test_near_limit_flag(cfg):
    res = screen_text("x" * cfg.chat.max_length, cfg.chat)
    assert res.ok and "near_limit_rate" in res.flags
