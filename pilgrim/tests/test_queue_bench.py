"""Queue benchmark rows (docs/systems/queue-bench.md, RADIO §6.1, §11, §14):
flagged requests use the real queue and leave nothing a listener can see.
Fakes only (hard rule 2)."""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from conftest import make_item
from fastapi.testclient import TestClient
from pilgrim.server import create_app, create_public_app
from pilgrim.tools.queue_bench import analyze, scenario
from test_producer import FakeSong, FakeVoice, make_song_producer

TOKEN = "bench-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class BenchSong(FakeSong):
    """Writes a real file, so the test can see the producer delete it."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.briefs_seen: list[dict] = []

    async def produce_song(self, brief, prefer="slow"):
        self.briefs_seen.append(dict(brief))
        self.path.write_bytes(b"audio")
        item = await super().produce_song(brief, prefer)
        return {**item, "media_path": str(self.path),
                "meta": {**item["meta"], "backend": "litellm", "generator": "fake"}}


# ------------------------------------------------------------------- store
def test_bench_rows_are_invisible_to_listeners_but_in_the_producer_queue(tmp_env):
    cfg, store, _ = tmp_env
    real = store.add_request("a real request", cap=None)
    free = store.add_bench_request("bench free", paid=False, opts={"run": "t"})
    paid = store.add_bench_request("bench paid", paid=True, opts={"run": "t"})

    board = store.request_board(50)
    assert [r["id"] for r in board["queue"]] == [real["id"]]
    assert store.request_counts() == (0, 1)
    assert store.waiting_free_requests() == 1
    assert [r["id"] for r in store.pending_requests()] == [real["id"]]
    # the producer still sees them, paid first
    assert [r["id"] for r in store.queued_requests()] == [paid["id"], real["id"], free["id"]]


def test_finish_and_purge(tmp_env):
    _, store, _ = tmp_env
    real = store.add_request("a real request", cap=None)
    a = store.add_bench_request("bench a", paid=False, opts={"run": "t"})
    b = store.add_bench_request("bench b", paid=False, opts={"run": "t"})
    store.cast_request_vote(a["id"], "bench-t-0-0", None, 1, max_voters_per_ip=3)
    store.cast_request_vote(real["id"], "someone", None, 1, max_voters_per_ip=3)
    store.finish_bench_request(a["id"], {"audio_s": 30.0})
    row = store.get_request(a["id"])
    assert row["status"] == "bench_done"
    assert json.loads(row["bench_opts"])["result"] == {"audio_s": 30.0}
    assert store.ready_request_songs() == []          # nothing to air

    store.mark_request_producing(b["id"])
    assert store.purge_bench_requests(apply=False) == {
        "requests": 2, "votes": 1, "producing_kept": 1}
    assert store.get_request(a["id"]) is not None     # dry run
    store.purge_bench_requests(apply=True)
    assert store.get_request(a["id"]) is None
    assert store.get_request(b["id"]) is not None     # in flight: kept
    assert store.get_request(real["id"]) is not None
    assert store.request_score(real["id"]) == 1       # a listener's vote survives


def test_finish_never_touches_a_listener_row(tmp_env):
    _, store, _ = tmp_env
    real = store.add_request("a real request", cap=None)
    store.finish_bench_request(real["id"], {"x": 1})
    assert store.get_request(real["id"])["status"] == "queued"


# ---------------------------------------------------------------- producer
def test_bench_song_is_rendered_then_thrown_away(cfg, tmp_env, tmp_path):
    cfg, store, _ = tmp_env
    for _ in range(cfg.inventory.fresh_songs_ready):
        make_item(cfg, store, "song", 120.0)
    before = len(store.list_items("song"))
    req = store.add_bench_request("bench song", paid=False,
                                  opts={"run": "t", "duration_s": 30})
    songs = BenchSong(tmp_path / "bench.mp3")
    voice = FakeVoice()
    prod = make_song_producer(cfg, store, voice, songs)
    assert asyncio.run(prod.song_step()) is True

    assert ("brief", "bench song") in songs.calls
    assert songs.briefs_seen[0]["max_duration_s"] == 30.0     # the short option
    assert not (tmp_path / "bench.mp3").exists()              # audio deleted
    assert len(store.list_items("song")) == before            # no library item
    assert store.list_items("intro") == []                    # no intro
    row = store.get_request(req["id"])
    assert row["status"] == "bench_done" and row["song_item_id"] is None
    result = json.loads(row["bench_opts"])["result"]
    assert result["generator"] == "fake" and result["reused_lyrics"] is False
    assert result["claimed"] <= result["write_start"] <= result["write_end"]
    assert result["write_end"] <= result["render_start"] <= result["render_end"]


def test_full_length_is_the_default(cfg, tmp_env, tmp_path):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    store.add_bench_request("bench song", paid=False, opts={"run": "t"})
    songs = BenchSong(tmp_path / "bench.mp3")
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    asyncio.run(prod.song_step())
    assert "max_duration_s" not in songs.briefs_seen[0]


def test_replay_reuses_the_original_words_without_the_brief_llm(cfg, tmp_env, tmp_path):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    orig = store.add_request("the original", cap=None)
    brief = {"title": "Old Song", "artist": "Old Band", "genre": "country-americana",
             "style_prompt": "bpm is 90", "lyrics": "[verse]\nold words",
             "seed": 7, "request_id": orig["id"]}
    song_id = make_item(cfg, store, "song", 120.0, meta={"brief": brief})
    store.mark_request_ready(orig["id"], song_id)
    assert store.request_song_brief(orig["id"])["lyrics"] == "[verse]\nold words"

    req = store.add_bench_request("the original", paid=False,
                                  opts={"run": "t", "replay_of": orig["id"]})
    songs = BenchSong(tmp_path / "bench.mp3")
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    asyncio.run(prod.song_step())
    assert not [c for c in songs.calls if c[0] == "brief"]    # no lyrics rewrite
    sent = songs.briefs_seen[0]
    assert sent["lyrics"] == "[verse]\nold words" and sent["request_id"] == req["id"]
    row = store.get_request(req["id"])
    assert json.loads(row["bench_opts"])["result"]["reused_lyrics"] is True


def test_a_failing_bench_song_fails_like_a_request_and_stays_flagged(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    req = store.add_bench_request("bench song", paid=True, opts={"run": "t"})
    prod = make_song_producer(cfg, store, FakeVoice(), FakeSong(fail=True))
    for _ in range(cfg.payments.max_attempts + 1):
        with contextlib.suppress(RuntimeError):
            asyncio.run(prod.song_step())
    row = store.get_request(req["id"])
    assert row["bench"] == 1 and row["status"] in ("failed", "queued")
    assert store.list_items("song") == []


# --------------------------------------------------------------------- API
def _app(tmp_env, monkeypatch):
    cfg = tmp_env[0]
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", TOKEN)
    app = create_app(cfg)
    return cfg, app, app.state.station


def test_admin_bench_add_list_purge(tmp_env, monkeypatch):
    cfg, app, station = _app(tmp_env, monkeypatch)
    c = TestClient(app)
    body = {"run": "t1", "requests": [
        {"text": "free one", "votes": 0},
        {"text": "free two", "votes": 3, "short": True},
        {"text": "paid one", "paid": True}]}
    assert c.post("/api/admin/bench", json=body).status_code == 401
    r = c.post("/api/admin/bench", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    ids = [x["id"] for x in r.json()["requests"]]
    # paid first, then the free row with votes, then FIFO
    assert [q["id"] for q in station.db.queued_requests()] == [ids[2], ids[1], ids[0]]

    listed = c.get("/api/admin/bench", params={"run": "t1"}, headers=AUTH).json()
    by_id = {x["id"]: x for x in listed["requests"]}
    assert by_id[ids[1]]["score"] == 3
    assert by_id[ids[1]]["opts"]["duration_s"] == cfg.bench.short_duration_s
    assert "duration_s" not in by_id[ids[0]]["opts"]
    assert set(listed["limits"]) == {"briefs", "render_slots", "free_request_slots"}
    assert c.get("/api/admin/bench", params={"run": "other"},
                 headers=AUTH).json()["requests"] == []

    # nothing a listener can see
    assert c.get("/api/requests").json()["queue"] == []

    assert c.delete("/api/admin/bench", headers=AUTH).json() == {
        "applied": False, "requests": 3, "votes": 3, "producing_kept": 0}
    c.delete("/api/admin/bench", params={"apply": "true"}, headers=AUTH)
    assert station.db.bench_requests() == []


def test_admin_bench_replay_and_validation(tmp_env, monkeypatch):
    cfg, app, station = _app(tmp_env, monkeypatch)
    c = TestClient(app)
    orig = station.db.add_request("an old request", cap=None)
    r = c.post("/api/admin/bench", headers=AUTH, json={
        "run": "t2", "requests": [{"replay_of": orig["id"]}]})
    assert r.status_code == 200
    row = station.db.get_request(r.json()["requests"][0]["id"])
    assert row["text"] == "an old request"
    assert json.loads(row["bench_opts"])["replay_of"] == orig["id"]

    bad = c.post("/api/admin/bench", headers=AUTH, json={
        "run": "t2", "requests": [{"text": "ok"}, {"replay_of": 999999}]})
    assert bad.status_code == 422
    assert len(station.db.bench_requests()) == 1      # the bad batch added nothing
    assert c.post("/api/admin/bench", headers=AUTH, json={
        "run": "bad name!", "requests": [{"text": "x"}]}).status_code == 422
    cfg.bench.max_batch = 1
    assert c.post("/api/admin/bench", headers=AUTH, json={
        "run": "t2", "requests": [{"text": "a"}, {"text": "b"}]}).status_code == 422
    cfg.bench.enabled = False
    assert c.post("/api/admin/bench", headers=AUTH, json={
        "run": "t2", "requests": [{"text": "a"}]}).status_code == 404


def test_admin_bench_not_on_public_app(tmp_env, monkeypatch):
    cfg, _, station = _app(tmp_env, monkeypatch)
    public = TestClient(create_public_app(cfg, station))
    assert public.get("/api/admin/bench", headers=AUTH).status_code == 404
    assert public.post("/api/admin/bench", headers=AUTH,
                       json={"run": "x", "requests": [{"text": "a"}]}).status_code == 404


# ------------------------------------------------------------------ report
def _row(i, *, paid=False, score=0, created=0.0, claimed=None, w=None, r=None,
         status="bench_done"):
    from datetime import UTC, datetime
    res = {}
    if claimed is not None:
        res["claimed"] = claimed
    if w:
        res.update(write_start=w[0], write_end=w[1])
    if r:
        res.update(render_start=r[0], render_end=r[1], audio_s=30.0, generator="g")
    return {"id": i, "paid": paid, "score": score, "status": status,
            "created_at": datetime.fromtimestamp(created, UTC).isoformat(),
            "claimed_at": None, "opts": {"result": res} if res else {}}


LIMITS = {"briefs": 3, "render_slots": 3, "free_request_slots": 2}


def test_analyze_passes_a_well_ordered_run():
    rows = [
        _row(1, score=0, created=0, claimed=30, w=(30, 40), r=(40, 100)),
        _row(2, score=2, created=0, claimed=10, w=(10, 20), r=(20, 80)),
        _row(3, paid=True, created=5, claimed=6, w=(6, 16), r=(16, 70)),
    ]
    a = analyze(rows, LIMITS)
    assert a["finished"] and all(c["ok"] for c in a["checks"].values())
    assert a["checks"]["renders_at_once"]["max"] == 3
    assert a["generators"] == {"g": 3}


def test_analyze_catches_each_broken_rule():
    # the low-score free row was claimed while a higher-score one waited
    votes = analyze([_row(1, score=0, created=0, claimed=5),
                     _row(2, score=4, created=0, claimed=9)], LIMITS)
    assert not votes["checks"]["votes_order_free_queue"]["ok"]
    # a free row was claimed after the paid row arrived, before the paid row
    paid = analyze([_row(1, created=0, claimed=20),
                    _row(2, paid=True, created=10, claimed=30)], LIMITS)
    assert not paid["checks"]["paid_before_waiting_free"]["ok"]
    # four renders at once on three slots, three free on two free slots
    over = analyze([_row(i, created=0, claimed=1, w=(1, 2), r=(2, 50))
                    for i in range(1, 5)], LIMITS)
    assert not over["checks"]["renders_at_once"]["ok"]
    assert not over["checks"]["free_renders_at_once"]["ok"]
    assert not over["checks"]["briefs_at_once"]["ok"]


def test_analyze_unfinished_run():
    a = analyze([_row(1, created=0, status="queued")], LIMITS)
    assert not a["finished"] and a["statuses"] == {"queued": 1}


def test_scenario_reverses_arrival_order_with_votes():
    first, second = scenario(3, 1, votes=True, short=True, replay=[55])
    assert [r.get("votes") for r in first[:3]] == [0, 1, 2]
    assert first[3] == {"replay_of": 55, "paid": False, "short": True, "reuse_lyrics": True}
    assert second == [{"text": second[0]["text"], "paid": True, "short": True}]
