"""Break structure (RADIO §5.2 "Break structure", `playout.breaks`): every
break between two songs is LEAD + PROMO, the lead set by the clock. Fakes
only; SimClock throughout."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from conftest import make_item, seed_pool
from pilgrim.config import RNG, SimClock
from pilgrim.scheduler import Scheduler
from pilgrim.selector import BreakSelector, PlayoutState, RandomSelector, make_selector
from test_producer import FakeVoice, make_producer


def _state(cfg, recent, **flags) -> PlayoutState:
    st = PlayoutState(cfg)
    st.available = {k: True for k in st.available}
    st.available["time_check"] = False
    st.news_valid = True
    st.recent_types = list(recent)
    for k, v in flags.items():
        if k == "unavailable":
            for t in v:
                st.available[t] = False
        else:
            setattr(st, k, v)
    return st


def _sel(cfg) -> BreakSelector:
    cfg.playout.breaks.enabled = True
    return BreakSelector(RNG(3), cfg)


# ---------------------------------------------------------------- selector
def test_make_selector_follows_the_switch(cfg):
    cfg.playout.breaks.enabled = True
    assert isinstance(make_selector(RNG(1), cfg), BreakSelector)
    cfg.playout.breaks.enabled = False
    assert isinstance(make_selector(RNG(1), cfg), RandomSelector)


def test_a_break_is_lead_then_promo_then_song(cfg):
    sel = _sel(cfg)
    lead = sel.choose_next(_state(cfg, ["liner", "promo", "song"]))
    assert lead in ("dj_talk", "commercial_break")          # a feature follows a light break
    item = "commercial" if lead == "commercial_break" else lead
    assert sel.choose_next(_state(cfg, ["song", item])) == "promo"
    assert sel.choose_next(_state(cfg, ["song", item, "promo"])) == "song"


def test_light_and_feature_breaks_take_turns(cfg):
    sel = _sel(cfg)
    assert sel.choose_next(_state(cfg, ["dj_talk", "promo", "song"])) == "liner"
    assert sel.choose_next(_state(cfg, ["commercial", "promo", "song"])) == "liner"
    assert sel.choose_next(_state(cfg, ["news", "promo", "song"])) == "liner"


def test_dj_talk_and_commercials_take_turns_as_the_feature(cfg):
    sel = _sel(cfg)
    hist = ["dj_talk", "promo", "song", "liner", "promo", "song"]
    assert sel.choose_next(_state(cfg, hist)) == "commercial_break"
    hist = ["commercial", "promo", "song", "liner", "promo", "song"]
    assert sel.choose_next(_state(cfg, hist)) == "dj_talk"
    # the one whose turn it is has no stock: the other steps in
    assert sel.choose_next(_state(cfg, hist, unavailable=["dj_talk"])) == "commercial_break"


def test_news_leads_the_break_on_its_mark_and_field_report_on_its_own(cfg):
    sel = _sel(cfg)
    hist = ["liner", "promo", "song"]
    assert sel.choose_next(_state(cfg, hist, news_due=True, field_due=True)) == "news"
    assert sel.choose_next(_state(cfg, hist, field_due=True)) == "field_report"
    # no bulletin ready: the break falls back to its ordinary lead, never waits
    assert sel.choose_next(_state(cfg, hist, news_due=True,
                                  unavailable=["news"])) in ("dj_talk", "commercial_break")


def test_no_promo_after_a_serious_bulletin(cfg):
    sel = _sel(cfg)
    assert sel.choose_next(_state(cfg, ["song", "news"],
                                  break_news_gravity="serious")) == "song"
    assert sel.choose_next(_state(cfg, ["song", "news"],
                                  break_news_gravity="normal")) == "promo"


def test_no_promo_stock_means_the_song_follows_the_lead(cfg):
    sel = _sel(cfg)
    assert sel.choose_next(_state(cfg, ["song", "liner"], unavailable=["promo"])) == "song"


def test_song_drought_hands_over_to_the_weighted_draw(cfg):
    sel = _sel(cfg)
    got = sel.choose_next(_state(cfg, ["song", "liner", "promo"], unavailable=["song"]))
    assert got != "song"


def test_never_two_songs_back_to_back_while_anything_can_be_said(cfg):
    sel = _sel(cfg)
    st = _state(cfg, ["liner", "promo", "song"],
                unavailable=["liner", "dj_talk", "commercial_break"])
    assert sel.choose_next(st) == "promo"       # no lead in stock: the promo still airs


# --------------------------------------------------------------- scheduler
def _pool(cfg, store):
    seed_pool(store, cfg, n_song=60, n_liner=40, n_com=25, n_dj=400)
    for kind in ("request_line", "paid_tier", "tree_fiddy", "campaign"):
        for _ in range(3):
            make_item(cfg, store, "promo", 9.0, meta={"kind": kind})
    for s in store.list_items("song"):
        store.add_item(type_="intro", media_path="intro.flac", duration_s=6.0,
                       evergreen=True, fresh=True, meta={"song_item_id": s["id"]})


def _run(cfg, store, hours, seed=5, on_step=None):
    cfg.playout.breaks.enabled = True
    clock = SimClock()
    sched = Scheduler(cfg, store, make_selector(RNG(seed), cfg), clock, RNG(seed))
    seen, last = [], 0
    wall0 = clock.wall()
    for _ in range(int(hours * 3600) // 30):
        clock.advance(30)
        if on_step:
            on_step(clock)
        sched.commit_lookahead()
        for r in store.program_after(last):
            seen.append((r["seq"], r["type"], r["duration_s"], r["item_id"]))
            last = max(last, r["seq"])
    return seen, wall0


def _breaks(seen):
    out, cur = [], None
    for _, t, d, iid in seen:
        if t == "song":
            if cur is not None:
                out.append(cur)
            cur = []
        elif cur is not None:
            cur.append((t, d, iid))
    return out


def test_every_break_has_a_promo_and_an_intro_and_lasts_15_to_45_s(cfg, tmp_env):
    _, store, _ = tmp_env
    _pool(cfg, store)
    seen, _ = _run(cfg, store, hours=6)
    breaks = _breaks(seen)
    assert len(breaks) > 100
    for b in breaks:
        types = [t for t, _, _ in b]
        assert types.count("promo") == 1, types
        assert types[-1] == "intro", types            # the song is introduced
        assert types[-2] == "promo", types            # the promo closes the break
        assert len(types) == 3, types                 # lead + promo + intro
        assert "time_check" not in types
        assert 15.0 <= sum(d for _, d, _ in b) <= 45.0, (types, sum(d for _, d, _ in b))


def test_leads_alternate_and_the_same_promo_is_not_repeated_closely(cfg, tmp_env):
    _, store, _ = tmp_env
    _pool(cfg, store)
    seen, _ = _run(cfg, store, hours=6)
    breaks = _breaks(seen)
    leads = [b[0][0] for b in breaks]
    light = {"liner", "station_id"}
    for a, b in zip(leads, leads[1:], strict=False):
        assert not (a in light and b in light), "two light breaks in a row"
        assert not (a not in light and b not in light), "two feature breaks in a row"
    features = [x for x in leads if x in ("dj_talk", "commercial")]
    assert all(a != b for a, b in zip(features, features[1:], strict=False))
    promos = [iid for b in breaks for t, _, iid in b if t == "promo"]
    gap = cfg.playout.breaks.promo_repeat_breaks
    for i, iid in enumerate(promos):
        assert iid not in promos[max(0, i - gap):i]
    kinds = {json.loads(store.get_item(i)["meta_json"])["kind"] for i in promos}
    assert kinds == {"request_line", "paid_tier", "tree_fiddy", "campaign"}


def test_news_airs_once_per_quarter_hour_and_field_reports_follow_the_half_hours(
        cfg, tmp_env):
    _, store, _ = tmp_env
    _pool(cfg, store)

    def refill(clock):   # the producer keeps one fresh bulletin and one field report
        wall = clock.wall()
        for type_ in ("news", "field_report"):
            if not any(i["fresh"] for i in store.list_items(type_)):
                store.add_item(
                    type_=type_, media_path=f"{type_}.flac", duration_s=18.0,
                    evergreen=False, fresh=True, gravity="normal",
                    expires_at=datetime.fromtimestamp(wall + 2700, UTC).isoformat())

    seen, wall0 = _run(cfg, store, hours=6, on_step=refill)
    # program time 0 is wall0: the scheduler starts its playhead at clock start
    t, news_at, field_at = 0.0, [], []
    start = seen[0][0]
    assert start == 1
    for _, type_, d, _ in seen:
        if type_ == "news":
            news_at.append((wall0 + t) % 3600)
        if type_ == "field_report":
            field_at.append((wall0 + t) % 3600)
        t += d
    br = cfg.playout.breaks
    assert 20 <= len(news_at) <= 26                    # ~4 an hour for 6 hours
    for s in news_at:
        d = min((s - m * 60 + 1800) % 3600 - 1800 for m in br.news_marks_min
                if -br.news_lead_s - 45 <= (s - m * 60 + 1800) % 3600 - 1800
                <= br.news_lag_s + 45)
        assert -br.news_lead_s - 45 <= d <= br.news_lag_s + 45
    assert 9 <= len(field_at) <= 13                    # ~2 an hour
    for s in field_at:
        assert any(-45 <= (s - m * 60 + 1800) % 3600 - 1800 <= br.field_window_s + 45
                   for m in br.field_marks_min), s
    types = [x[1] for x in seen if x[1] != "intro"]
    for a, b in zip(types, types[1:], strict=False):
        assert {a, b} != {"news", "field_report"}      # never adjacent


def test_weighted_draw_is_untouched_when_the_switch_is_off(cfg, tmp_env):
    _, store, _ = tmp_env
    _pool(cfg, store)
    cfg.playout.breaks.enabled = False
    clock = SimClock()
    sched = Scheduler(cfg, store, make_selector(RNG(5), cfg), clock, RNG(5))
    assert sched._break_mode is False
    cfg.playout.breaks.enabled = True
    sched2 = Scheduler(cfg, store, RandomSelector(RNG(5), cfg), SimClock(), RNG(5))
    assert sched2._break_mode is False                 # old selector: old rules


# ---------------------------------------------------------------- producer
def test_song_intros_are_backfilled_and_kept(cfg, tmp_env):
    _, store, _ = tmp_env
    cfg.inventory.intro_backfill_per_pass = 2
    ids = [store.add_item(type_="song", media_path=f"s{i}.mp3", duration_s=120.0,
                          title=f"Song {i}", artist="The Band", evergreen=True)
           for i in range(5)]
    voice = FakeVoice()
    prod = make_producer(cfg, store, voice)
    asyncio.run(prod.ensure_song_intros())
    assert len(store.list_items("intro")) == 2          # a few per pass
    for _ in range(5):
        asyncio.run(prod.ensure_song_intros())
    intros = store.list_items("intro")
    assert sorted(json.loads(i["meta_json"])["song_item_id"] for i in intros) == ids
    assert all(i["evergreen"] for i in intros)          # retained, replayed
    assert all("request_id" not in json.loads(i["meta_json"]) for i in intros)
    asyncio.run(prod.ensure_song_intros())
    assert len(store.list_items("intro")) == 5          # nothing made twice
    assert prod._intro_check_after > prod.clock.now()   # done: stop re-reading the tables


def test_intro_backfill_stands_down_while_a_request_is_in_flight(cfg, tmp_env):
    _, store, _ = tmp_env
    cfg.inventory.intro_backfill_per_pass = 2
    store.add_item(type_="song", media_path="s.mp3", duration_s=120.0,
                   title="Song", artist="The Band", evergreen=True)
    req = store.add_request("a song please", cap=None)
    prod = make_producer(cfg, store, FakeVoice())
    asyncio.run(prod.ensure_song_intros())
    assert store.list_items("intro") == []              # the queue comes first
    store.mark_request_producing(req["id"])
    store.mark_request_ready(req["id"], None)
    asyncio.run(prod.ensure_song_intros())
    assert len(store.list_items("intro")) == 1          # quiet again: carry on


def test_intro_backfill_is_off_by_default_and_skips_request_intros(cfg, tmp_env):
    _, store, _ = tmp_env
    sid = store.add_item(type_="song", media_path="s.mp3", duration_s=120.0,
                         title="Song", artist="The Band", evergreen=True)
    cfg.inventory.intro_backfill_per_pass = 0
    prod = make_producer(cfg, store, FakeVoice())
    asyncio.run(prod.ensure_song_intros())
    assert store.list_items("intro") == []
    # a one-shot request intro is not a reusable one: the song still gets its own
    store.add_item(type_="intro", media_path="i.mp3", duration_s=7.0, evergreen=False,
                   meta={"song_item_id": sid, "request_id": 9})
    cfg.inventory.intro_backfill_per_pass = 2
    asyncio.run(prod.ensure_song_intros())
    assert len(store.list_items("intro")) == 2


def _turnover(prod) -> None:
    prod._turnover_check_after = 0.0     # as if the 10-minute rest had passed
    prod.ensure_turnover()


def _aged(store, type_, n, plays, age_s, clock):
    ids = []
    for k in range(n):
        iid = store.add_item(
            type_=type_, media_path=f"{type_}{k}.mp3", duration_s=15.0, evergreen=True,
            created_at=datetime.fromtimestamp(clock.wall() - age_s, UTC).isoformat())
        for _ in range(plays + k):
            store.commit_song_air(iid, clock.wall() - 100)
        ids.append(iid)
    return ids


def test_turnover_retires_the_most_played_spot_and_then_waits(cfg, tmp_env):
    _, store, _ = tmp_env
    cfg.inventory.commercials_min = 4
    cfg.inventory.commercials_turnover_per_day = 2
    cfg.inventory.liners_turnover_per_day = 0
    cfg.inventory.turnover_min_plays = 10
    # the store stamps new items with the real time, so the sim clock starts there
    clock = SimClock(epoch=time.time())
    ids = _aged(store, "commercial", 4, plays=12, age_s=5 * 86400, clock=clock)
    prod = make_producer(cfg, store, FakeVoice(), clock_override=clock)
    _turnover(prod)
    assert store.get_item(ids[-1])["retired"] == 1       # the most played one
    assert store.count_usable_of_type("commercial") == 3
    _turnover(prod)                               # below target: nothing more
    assert store.count_usable_of_type("commercial") == 3
    asyncio.run(prod.ensure_commercials())               # the producer replaces it
    assert store.count_usable_of_type("commercial") == 4
    _turnover(prod)                               # a new one exists: wait 12 h
    assert store.count_usable_of_type("commercial") == 4
    clock.advance(12 * 3600 + 60)
    _turnover(prod)
    assert store.count_usable_of_type("commercial") == 3


def test_turnover_leaves_barely_played_and_emergency_items_alone(cfg, tmp_env):
    _, store, _ = tmp_env
    cfg.inventory.commercials_min = 2
    cfg.inventory.commercials_turnover_per_day = 2
    cfg.inventory.turnover_min_plays = 10
    clock = SimClock()
    _aged(store, "commercial", 2, plays=1, age_s=5 * 86400, clock=clock)
    prod = make_producer(cfg, store, FakeVoice(), clock_override=clock)
    _turnover(prod)
    assert store.count_usable_of_type("commercial") == 2
    cfg.inventory.commercials_turnover_per_day = 0       # off
    _aged(store, "commercial", 1, plays=50, age_s=5 * 86400, clock=clock)
    _turnover(prod)
    assert store.count_usable_of_type("commercial") == 3


def test_no_clock_reads_are_made_under_the_break_structure(cfg, tmp_env):
    _, store, _ = tmp_env
    cfg.playout.breaks.enabled = True
    prod = make_producer(cfg, store, FakeVoice())
    asyncio.run(prod.ensure_time_checks())
    assert store.list_items("time_check") == []
