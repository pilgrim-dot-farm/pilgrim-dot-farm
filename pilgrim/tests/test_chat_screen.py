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
    # the source file itself contains no term, only digests and code
    import inspect

    src = inspect.getsource(prohibited)
    assert "nigger" not in src.lower()
    assert "faggot" not in src.lower()


def test_digest_list_hit_rejects(monkeypatch):
    phrase = "barnyard menace"
    monkeypatch.setattr(prohibited, "PROHIBITED_DIGESTS",
                        frozenset({hashlib.sha256(
                            prohibited.normalize(phrase).encode()).hexdigest()}))
    assert prohibited.prohibited_hit("that barnyard menace again")
    assert not prohibited.prohibited_hit("barnyard friends")


def test_near_miss_and_leetspeak_pass_layer_a(cfg):
    # Token-exact hashed matching: evasions are Layer B's job (documented).
    assert not prohibited.prohibited_hit("classic tractor")
    assert not prohibited.prohibited_hit("g1les")
    assert screen_text("classic tractor", cfg.chat).ok
    assert screen_text("g1les was here", cfg.chat).ok


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
