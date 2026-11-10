"""Instagram Reels posting (INSTAGRAM.md Steps 1-5), fakes only (AGENTS §1.2).

Real ffmpeg runs over tiny generated inputs (a CLI tool, not a backend); the
Graph API, S3, CloudFront and the LLM are fakes.
"""
from __future__ import annotations

from pilgrim.config import load_secret


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
