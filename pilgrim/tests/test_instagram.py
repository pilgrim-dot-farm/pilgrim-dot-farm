"""Instagram Reels posting (INSTAGRAM.md Steps 1-5), fakes only (AGENTS §1.2).

Real ffmpeg runs over tiny generated inputs (a CLI tool, not a backend); the
Graph API, S3, CloudFront and the LLM are fakes.
"""
from __future__ import annotations

import json

import pytest
from pilgrim import instagram as ig
from pilgrim.config import load_secret
from pilgrim.instagram import (
    CaptionWriter,
    compose_caption,
    line_problem,
    tagline_bank,
)


# ---------------------------------------------------------------- Step 1
def test_instagram_config_defaults(cfg):
    i = cfg.instagram
    assert cfg.hosts.graph == "https://graph.facebook.com"
    assert cfg.models.instagram_caption
    assert i.api_version.startswith("v") and i.layout == "square"
    assert i.clip_s == 20.0 and i.key_prefix == "reels/"
    assert i.share_to_feed is True and i.ai_label is True
    assert i.hashtags == ["#ai", "#aimusic", "#aimusicgenerator", "#pilgrimdotfarm"]
    assert "https://pilgrim.farm" in i.cta


def test_tests_never_see_the_instagram_secrets():
    assert load_secret("INSTAGRAM_TOKEN") == ""
    assert load_secret("INSTAGRAM_ACCOUNT_ID") == ""


# ---------------------------------------------------------------- Step 3
BANK_MD = """# Station Taglines — operator creative notes

1. Pilgrim Dot Farm. Our field, your dreams.
2. We don't have a record label. We have a business license and poor impulse
   control.
3. The only thing organic around here is the bullshit.

## Second bank — 2026-11-06 (operator, verbatim)

10. Our DJ has never been born, never been paid, and never called in sick.
11. We're not replacing musicians. We're giving terrible ideas a fighting
    chance.
"""


def test_tagline_bank_joins_continuations_and_drops_profanity():
    assert tagline_bank(BANK_MD) == [
        "Pilgrim Dot Farm. Our field, your dreams.",
        "We don't have a record label. We have a business license and poor impulse control.",
        "Our DJ has never been born, never been paid, and never called in sick.",
        "We're not replacing musicians. We're giving terrible ideas a fighting chance.",
    ]


def test_the_real_bank_parses_and_every_line_is_a_valid_caption(cfg):
    bank = ig.load_bank(cfg)
    assert len(bank) >= 40
    assert not any("bullshit" in t for t in bank)
    rejected = [(t, line_problem(t, "")) for t in bank if line_problem(t, "")]
    assert rejected == []


def test_line_problem_catalogue():
    assert line_problem("A perfectly fine deadpan line about a possum.", "x") is None
    assert line_problem("short", "x") == "length 5"
    assert line_problem("Two lines\nof text that is long enough", "x") == "multi-line"
    assert line_problem("Dinosaurs stomped the diorama. #extinct", "x") == "hashtag/handle/markdown"
    assert line_problem("Follow us for more of this nonsense, @pilgrim", "x") == \
        "hashtag/handle/markdown"
    assert line_problem("Visit www.pilgrim.farm for your own song today", "x") == "url/contact"
    assert line_problem("Request yours at pilgrim.farm, link in bio", "x") == "url/contact"
    assert line_problem("Roar \U0001F996 roar roar roar roar roar", "x") == "emoji"
    assert line_problem("One. Two. Three. Four. Five sentences is too many here.", "x") == \
        "more than three sentences"
    assert line_problem("T-Rex at the Science Fair", "T-Rex at the Science Fair") == \
        "restates the title"
    assert line_problem("The only thing organic around here is the bullshit.", "x") == \
        "profanity"


def test_compose_caption_is_deterministic_after_the_line(cfg):
    cap = compose_caption(cfg, "Our field, your dreams.", "Possum Divorce",
                          "The Marsupial Settlement")
    assert cap == ("Our field, your dreams.\n\n"
                   "“Possum Divorce” by The Marsupial Settlement. "
                   "Request your own at https://pilgrim.farm — link in bio.\n\n"
                   "#ai #aimusic #aimusicgenerator #pilgrimdotfarm")
    cfg.instagram.name_the_song = False
    assert compose_caption(cfg, "L" * 20, "T", "A").startswith("L" * 20 + "\n\nRequest your own")
    with pytest.raises(ValueError):
        compose_caption(cfg, "x" * 2300, "T", "A")


class _LLM:
    """Scripted answers; records the (model, system, user) of each call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def chat_json(self, model, system, user, max_tokens=None, temperature=0.8):
        self.calls.append((model, system, user))
        return self.answers.pop(0)


def _song(**over):
    row = {"id": 9188, "type": "song", "title": "T-Rex at the Science Fair",
           "artist": "The Cretaceous Choir", "genre": "pop-alternative-rock",
           "media_path": "/lib/song_1.mp3", "s3_key": "audio/song/song_1-ab.mp3",
           "art_s3_key": "art/song_1-cd.jpg", "duration_s": 282.0, "retired": 0,
           "meta_json": json.dumps({
               "lyrics": "[chorus]\nT-Rex at the science fair\nStomping down the hall",
               "request_text": "A song about dinosaurs"})}
    row.update(over)
    return row


async def test_caption_writer_takes_a_valid_line_and_feeds_the_bank(cfg):
    llm = _LLM([{"line": "Sixty-six million years of extinction, undone for a diorama.",
                 "tagline": 2}])
    w = CaptionWriter(cfg, llm, "Taglines:\n{taglines}\nJSON please.", tagline_bank(BANK_MD))
    line, source, tag = await w.write(_song(), note="mention the glitter")
    assert (line, source, tag) == (
        "Sixty-six million years of extinction, undone for a diorama.", "llm", 2)
    model, system, user = llm.calls[0]
    assert model == cfg.models.instagram_caption
    assert "1. Pilgrim Dot Farm. Our field, your dreams." in system
    assert "{taglines}" not in system
    assert "Title: T-Rex at the Science Fair" in user
    assert "Operator note" in user and "mention the glitter" in user
    assert "untrusted data" in user and "A song about dinosaurs" in user
    assert "Stomping down the hall" in user


async def test_caption_writer_rejects_bad_lines_then_falls_back_to_a_verbatim_tagline(cfg):
    cfg.instagram.caption_attempts = 4
    bank = tagline_bank(BANK_MD)
    llm = _LLM([
        {"line": "Dinosaurs! #extinct #ai"},                          # hashtags
        {"line": "Request yours at pilgrim.farm, link in bio"},       # url-ish
        {"line": "T-Rex at the Science Fair"},                        # restates the title
        {"line": "Roar \U0001F996 roar roar roar roar roar roar"},    # emoji
    ])
    w = CaptionWriter(cfg, llm, "{taglines}", bank)
    line, source, tag = await w.write(_song(id=9188))
    assert source == "fallback"
    assert line == bank[9188 % len(bank)] and tag == 9188 % len(bank) + 1
    assert len(llm.calls) == 4


async def test_caption_writer_survives_a_model_that_errors(cfg):
    class Boom:
        async def chat_json(self, *a, **k):
            raise RuntimeError("llm down")

    w = CaptionWriter(cfg, Boom(), "{taglines}", tagline_bank(BANK_MD))
    line, source, _ = await w.write(_song())
    assert source == "fallback" and line_problem(line, "") is None
