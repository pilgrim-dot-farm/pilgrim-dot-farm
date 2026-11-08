"""The three-box paid request parts (PROMPT_UPGRADE.md Step 1).

Pure functions only: composition, the song-only view, and the row round trip.
"""
from __future__ import annotations

from pilgrim.pipelines.request_parts import RequestParts, from_row, is_label_line, normalise


def test_composed_omits_empty_parts_and_keeps_order():
    parts = RequestParts(lyrics="A raccoon for president.", music="Death metal.")
    assert parts.composed() == ("Lyrics:\nA raccoon for president.\n\n"
                                "Music:\nDeath metal.")
    assert RequestParts().composed() == ""
    assert RequestParts(art="A chrome vending machine.").composed() == (
        "Album art:\nA chrome vending machine.")


def test_composed_round_trips_through_normalise():
    parts = RequestParts(lyrics="one\n\ntwo", music="loud", art="moonlit cliff")
    composed = parts.composed()
    assert normalise(composed) == composed
    assert normalise(composed.replace("\n", "\r\n")) == composed


def test_song_text_never_contains_art():
    parts = RequestParts(lyrics="Words", music="Sound", art="SECRET ART TEXT")
    song = parts.song_text()
    assert "Words" in song and "Sound" in song
    assert "SECRET ART TEXT" not in song
    assert "Album art:" not in song


def test_subject_text_prefers_lyrics():
    assert RequestParts(lyrics="words", music="sound").subject_text() == "words"
    assert RequestParts(music="sound").subject_text() == "sound"
    assert RequestParts(art="art").subject_text() == ""


def test_has_song_requires_lyrics_or_music():
    assert RequestParts(lyrics="x").has_song
    assert RequestParts(music="x").has_song
    assert not RequestParts(art="x").has_song
    assert not RequestParts().has_song


def test_from_row_none_for_legacy_rows():
    assert from_row(None) is None
    assert from_row({}) is None
    assert from_row({"lyrics_text": None, "music_text": None,
                     "art_text": None}) is None


def test_from_row_reads_parts_and_nulls_as_empty():
    parts = from_row({"lyrics_text": "words", "music_text": None,
                      "art_text": ""})
    assert parts == RequestParts(lyrics="words", music="", art="")


def test_label_line_detection():
    assert is_label_line("Lyrics:")
    assert is_label_line("  Album art:  ")
    assert not is_label_line("Lyrics: A raccoon")
    assert not is_label_line("")
