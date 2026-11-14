"""Queue benchmark on the live station (operator tool, docs/systems/queue-bench.md).

Adds flagged benchmark requests through the admin listener, then reads their
timings back and checks the queue rules (RADIO §6.1):

  1. votes order the free requests that are still waiting;
  2. a paid request goes ahead of every free request not yet being worked on;
  3. no more briefs are written at once than the brief model allows;
  4. no more songs render at once than there are generator slots.

Benchmark songs are REAL renders on the production pool (AGENTS rule 6,
operator exception): full length by default, `--short` for 30 s. Their audio is
deleted; nothing enters the library, airs, or shows on the public board. They
do share the queue with listeners while a run is in flight.

    python -m pilgrim.tools.queue_bench run --name tue1 --free 6 --paid 2 --short
    python -m pilgrim.tools.queue_bench run --name tue2 --replay-last 4
    python -m pilgrim.tools.queue_bench report --name tue1 [--wait]
    python -m pilgrim.tools.queue_bench purge            # dry run
    python -m pilgrim.tools.queue_bench purge --apply
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from typing import Any

import httpx

from pilgrim.config import load_config
from pilgrim.dj_security import load_dj_token

PROMPTS = [
    "a slow waltz about a barn cat who runs the farm at night",
    "an upbeat disco song about a tractor that will not start",
    "a sea shanty for a pond full of very proud ducks",
    "a country ballad about losing a boot in the mud",
    "a big band number about the first frost of the year",
    "a gospel song about a rooster who is always early",
    "a synth pop song about counting sheep and losing count",
    "a folk song about a scarecrow who wants to travel",
    "a blues about a fence that keeps falling over",
    "a pop anthem about the last pumpkin in the patch",
]
DONE = ("bench_done", "failed")


def scenario(free: int, paid: int, *, votes: bool, short: bool,
             replay: list[int], reuse_lyrics: bool = True) -> tuple[list[dict], list[dict]]:
    """(first batch, paid batch). Free request k gets k up-votes, so the vote
    order is the exact reverse of the arrival order: if votes work, the last
    free request is written first. Paid requests are a second batch, sent
    after the free ones are already in the queue."""
    first: list[dict[str, Any]] = []
    for k in range(free):
        first.append({"text": PROMPTS[k % len(PROMPTS)], "paid": False,
                      "votes": min(k, 20) if votes else 0, "short": short})
    for rid in replay:
        first.append({"replay_of": rid, "paid": False, "short": short,
                      "reuse_lyrics": reuse_lyrics})
    second = [{"text": PROMPTS[-(k % len(PROMPTS)) - 1], "paid": True, "short": short}
              for k in range(paid)]
    return first, second


def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _max_overlap(spans: list[tuple[float, float]]) -> int:
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans],
                    key=lambda e: (e[0], e[1]))
    best = cur = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def analyze(rows: list[dict], limits: dict[str, int]) -> dict[str, Any]:
    """Pure: benchmark rows (GET /api/admin/bench) -> checks + timings."""
    for r in rows:
        res = (r.get("opts") or {}).get("result") or {}
        r["_res"] = res
        r["_created"] = _ts(r.get("created_at"))
        r["_claimed"] = res.get("claimed") or _ts(r.get("claimed_at"))
    claimed = [r for r in rows if r["_claimed"] is not None]
    done = [r for r in rows if r["status"] == "bench_done"]

    # 1. votes: when A was claimed, no free row still waiting had a higher score
    vote_bad = []
    free = [r for r in claimed if not r["paid"]]
    for a in free:
        for b in rows:
            if b["paid"] or b["id"] == a["id"] or b["_created"] is None:
                continue
            waiting = b["_created"] <= a["_claimed"] and (
                b["_claimed"] is None or b["_claimed"] > a["_claimed"])
            if waiting and b["score"] > a["score"]:
                vote_bad.append({"claimed": a["id"], "score": a["score"],
                                 "waiting": b["id"], "waiting_score": b["score"]})
    # 2. paid: no free row is claimed after a paid row arrived and before that paid row
    paid_bad = []
    for p in rows:
        if not p["paid"] or p["_created"] is None:
            continue
        for f in free:
            if f["_claimed"] > p["_created"] and (
                    p["_claimed"] is None or f["_claimed"] < p["_claimed"]):
                paid_bad.append({"paid": p["id"], "free_claimed_first": f["id"]})
    # 3 + 4. concurrency
    writes = [(r["_res"]["write_start"], r["_res"]["write_end"]) for r in done
              if not r["_res"].get("reused_lyrics") and "write_end" in r["_res"]]
    renders = [(r["_res"]["render_start"], r["_res"]["render_end"]) for r in done
               if "render_end" in r["_res"]]
    free_renders = [(r["_res"]["render_start"], r["_res"]["render_end"]) for r in done
                    if "render_end" in r["_res"] and not r["paid"]]
    max_w, max_r, max_fr = _max_overlap(writes), _max_overlap(renders), _max_overlap(free_renders)

    def stat(vals: list[float]) -> dict[str, float] | None:
        if not vals:
            return None
        return {"n": len(vals), "median": round(statistics.median(vals), 1),
                "max": round(max(vals), 1)}

    write_s = [b - a for a, b in writes]
    render_s = [b - a for a, b in renders]
    slot_wait = [r["_res"]["render_start"] - r["_res"]["write_end"] for r in done
                 if "write_end" in r["_res"] and "render_start" in r["_res"]]
    total = [r["_res"]["render_end"] - r["_created"] for r in done
             if "render_end" in r["_res"] and r["_created"] is not None]
    audio = [float(r["_res"].get("audio_s") or 0) for r in done]
    span = (max(b for _, b in renders) - min(r["_created"] for r in done)) if renders else 0.0
    gens: dict[str, int] = {}
    for r in done:
        g = str(r["_res"].get("generator") or r["_res"].get("backend") or "?")
        gens[g] = gens.get(g, 0) + 1
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {
        "rows": len(rows), "statuses": statuses,
        "finished": all(r["status"] in DONE for r in rows) and bool(rows),
        "checks": {
            "votes_order_free_queue": {"ok": not vote_bad, "violations": vote_bad[:10]},
            "paid_before_waiting_free": {"ok": not paid_bad, "violations": paid_bad[:10]},
            "briefs_at_once": {"ok": max_w <= limits.get("briefs", 1), "max": max_w,
                               "limit": limits.get("briefs", 1)},
            "renders_at_once": {"ok": max_r <= limits.get("render_slots", 1), "max": max_r,
                                "limit": limits.get("render_slots", 1)},
            "free_renders_at_once": {
                "ok": max_fr <= max(limits.get("free_request_slots", 1), 1),
                "max": max_fr, "limit": limits.get("free_request_slots", 1)},
        },
        "timings_s": {"write": stat(write_s), "wait_for_slot": stat(slot_wait),
                      "render": stat(render_s), "request_to_done": stat(total)},
        "audio_s": round(sum(audio), 1),
        "songs_per_hour": round(len(done) / span * 3600, 1) if span > 0 else None,
        "generators": gens,
        "failed": [r["id"] for r in rows if r["status"] == "failed"],
    }


def print_report(name: str, a: dict[str, Any]) -> None:
    print(f"run {name}: {a['rows']} requests  {a['statuses']}"
          + ("" if a["finished"] else "  (still running)"))
    for key, c in a["checks"].items():
        extra = f"  max {c['max']} / limit {c['limit']}" if "max" in c else ""
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {key}{extra}")
        for v in c.get("violations") or []:
            print(f"        {v}")
    for key, st in a["timings_s"].items():
        if st:
            print(f"  {key:<16} median {st['median']}s  max {st['max']}s  (n={st['n']})")
    print(f"  audio rendered   {a['audio_s']}s   songs/hour {a['songs_per_hour']}   "
          f"generators {a['generators']}")
    if a["failed"]:
        print(f"  failed requests: {a['failed']}")


class Admin:
    def __init__(self) -> None:
        cfg = load_config()
        self.base = f"http://{cfg.station.admin_host}:{cfg.station.admin_port}"
        self.headers = {"Authorization": f"Bearer {load_dj_token()}"}

    def call(self, method: str, **kw: Any) -> dict[str, Any]:
        r = httpx.request(method, self.base + "/api/admin/bench", headers=self.headers,
                          timeout=30.0, **kw)
        if r.status_code >= 400:
            sys.exit(f"{method} /api/admin/bench -> {r.status_code}: {r.text[:300]}")
        return dict(r.json())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="queue_bench", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="add benchmark requests to the live queue")
    run.add_argument("--name", required=True)
    run.add_argument("--free", type=int, default=0)
    run.add_argument("--paid", type=int, default=0)
    run.add_argument("--no-votes", action="store_true")
    run.add_argument("--short", action="store_true",
                     help="render bench.short_duration_s (30 s) instead of full length")
    run.add_argument("--replay", default="", help="comma-separated past request ids")
    run.add_argument("--replay-last", type=int, default=0,
                     help="replay the N newest aired listener requests")
    run.add_argument("--rewrite-lyrics", action="store_true",
                     help="replays ask the brief LLM again instead of reusing the words")
    run.add_argument("--paid-after", type=float, default=30.0,
                     help="seconds to wait before adding the paid requests")
    run.add_argument("--yes", action="store_true")
    rep = sub.add_parser("report", help="check the queue rules and print timings")
    rep.add_argument("--name", required=True)
    rep.add_argument("--wait", action="store_true", help="poll until the run finishes")
    rep.add_argument("--json", action="store_true")
    pur = sub.add_parser("purge", help="delete benchmark rows (dry run unless --apply)")
    pur.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    admin = Admin()

    if args.cmd == "run":
        replay = [int(x) for x in args.replay.split(",") if x.strip()]
        if args.replay_last:
            cands = admin.call("GET", params={"run": args.name,
                                              "candidates": args.replay_last})["candidates"]
            replay += [c["id"] for c in cands]
        first, second = scenario(args.free, args.paid, votes=not args.no_votes,
                                 short=args.short, replay=replay,
                                 reuse_lyrics=not args.rewrite_lyrics)
        if not first and not second:
            ap.error("nothing to add: give --free, --paid, --replay or --replay-last")
        length = "30 s" if args.short else "FULL LENGTH"
        print(f"run {args.name}: {len(first)} free/replay + {len(second)} paid, "
              f"{length} real renders on the production pool")
        if not args.yes and input("add them to the live queue? [y/N] ").lower() != "y":
            return 1
        if first:
            print(json.dumps(admin.call("POST", json={"run": args.name, "requests": first})))
        if second:
            if first and args.paid_after > 0:
                time.sleep(args.paid_after)
            print(json.dumps(admin.call("POST", json={"run": args.name, "requests": second})))
        print(f"watch it: python -m pilgrim.tools.queue_bench report --name {args.name} --wait")
        return 0
    if args.cmd == "report":
        while True:
            body = admin.call("GET", params={"run": args.name})
            a = analyze(body["requests"], body["limits"])
            if a["finished"] or not args.wait:
                break
            print(f"  {a['statuses']}", flush=True)
            time.sleep(30)
        if args.json:
            print(json.dumps(a, indent=1))
        else:
            print_report(args.name, a)
        return 0 if all(c["ok"] for c in a["checks"].values()) else 1
    print(json.dumps(admin.call("DELETE", params={"apply": str(args.apply).lower()})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
