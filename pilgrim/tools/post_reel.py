"""Post an aired song to Instagram Reels (INSTAGRAM.md Step 5; RADIO §8.4).

Operator- or agent-triggered, never a station task. Three modes:

    python -m pilgrim.tools.post_reel --find "possum"         # list matching songs; nothing else
    python -m pilgrim.tools.post_reel --item 9188             # build + caption + upload; no post
    python -m pilgrim.tools.post_reel --item 9188 --publish   # then container -> publish

`--note TEXT` guides the caption line; `--caption TEXT` is the line, verbatim
(the song line, call to action and hashtags are still appended); `--force`
reposts a song this account already has. The last stdout line is always
`RESULT {json}` for the calling agent (docs/instagram.md).

Exit codes: 0 done, 1 failed, 2 bad arguments / no such song, 3 already posted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pilgrim import instagram as ig
from pilgrim.config import Config, load_api_key, load_config
from pilgrim.db import open_store
from pilgrim.media_publish import CACHE_CONTROL, ObjectStore, S3ObjectStore, sha256_file
from pilgrim.pipelines.llm import LLM
from pilgrim.store import Store
from pilgrim.tools import make_reel

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_POSTED = 0, 1, 2, 3


def _head(url: str) -> httpx.Response:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        return c.head(url)


@dataclass
class Deps:
    """Everything with a network behind it, so tests inject fakes (AGENTS §1.2)."""
    s3: ObjectStore
    llm: Any                                  # LLM or a fake; None with --caption
    graph: ig.GraphClient | None = None       # None until --publish
    get: Callable[[str], bytes] = make_reel.downloader
    head: Callable[[str], httpx.Response] = _head


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _result(status: str, **fields: Any) -> dict[str, Any]:
    out = {"status": status, **fields}
    print("RESULT " + json.dumps(out, ensure_ascii=False))
    return out


def find(store: Store, q: str, limit: int = 25) -> list[dict[str, Any]]:
    """Songs whose title or artist contains `q` (case-insensitive), with the
    state the agent needs to confirm a choice (D1)."""
    rows, _, _ = store.song_catalogue(q, "title", "asc", None, limit)
    out = []
    for r in rows:
        item = store.get_item(int(r["id"])) or {}
        prior = ig.posted_record(store, int(r["id"])) or {}
        out.append({"id": int(r["id"]), "title": r.get("title") or "",
                    "artist": r.get("artist") or "",
                    "duration_s": float(r.get("duration_s") or 0),
                    "postable": bool(item.get("s3_key") and item.get("art_s3_key")),
                    "posted": prior.get("permalink") or (prior.get("media_id") and "yes") or None})
    return out


def print_matches(matches: list[dict[str, Any]], q: str) -> None:
    if not matches:
        print(f"no songs match {q!r}")
        return
    for m in matches:
        mm, ss = divmod(int(m["duration_s"]), 60)
        flags = "" if m["postable"] else "  [not postable: audio or cover missing in S3]"
        posted = f"  POSTED {m['posted']}" if m["posted"] else ""
        print(f"{m['id']:>6}  {m['title']} — {m['artist']}  ({mm}:{ss:02d}){flags}{posted}")
    print(f"{len(matches)} match(es). Confirm the full title AND artist with the operator, "
          f"then pass --item ID.")


def _unpostable(item: dict[str, Any] | None, item_id: int) -> str | None:
    if not item or item.get("type") != "song":
        return f"no song {item_id}"
    if item.get("retired"):
        return f"song {item_id} is retired"
    if not (item.get("title") and item.get("artist")):
        return f"song {item_id} has no title/artist"
    if not item.get("s3_key") or not item.get("art_s3_key"):
        return f"song {item_id} is not fully published to S3 (audio and cover art both needed)"
    return None


async def run(cfg: Config, store: Store, args: argparse.Namespace, deps: Deps) -> int:
    item = store.get_item(args.item)
    why = _unpostable(item, args.item)
    if why:
        print(why, file=sys.stderr)
        _result("usage", error=why)
        return EXIT_USAGE
    assert item is not None
    title, artist = str(item["title"]), str(item["artist"])
    prior = ig.posted_record(store, args.item)
    if prior and not args.force:
        print(f"already posted: {prior.get('permalink') or prior.get('media_id')} "
              f"at {prior.get('posted_at')}; pass --force to repost")
        _result("already_posted", item_id=args.item, title=title, artist=artist, prior=prior)
        return EXIT_POSTED

    layout = cfg.instagram.layout
    ledger = ig.Ledger(make_reel.output_path(cfg, item, layout).with_suffix(".json"))
    ledger.update(item_id=args.item, title=title, artist=artist, status="building",
                  created_at=_now(), layout=layout)
    try:
        # 1. the clip: ffmpeg over the CloudFront copies (ALBUM_ART.md Step 11, Step 2)
        clip = float(args.duration or cfg.instagram.clip_s)
        clip = min(clip, float(item["duration_s"] or 0) - 0.5)
        if clip < 3.0:
            raise RuntimeError(f"song too short for a Reel ({item['duration_s']}s)")
        video = make_reel.run(cfg, item, start=args.start, duration=clip,
                              get=deps.get, layout=layout)
        info = make_reel.probe(video)
        size = video.stat().st_size
        ledger.update(status="built", video={
            "path": str(video), "bytes": size,
            "duration_s": float(info["format"]["duration"]),
            "clip_s": clip, "start_s": args.start})

        # 2. the caption: one LLM line, deterministic tail (Step 3)
        if args.caption:
            line, source, tag = args.caption.strip(), "operator", None
        else:
            writer = ig.CaptionWriter(cfg, deps.llm, ig.load_prompt(cfg), ig.load_bank(cfg))
            line, source, tag = await writer.write(item, note=args.note)
        caption = ig.compose_caption(cfg, line, title, artist)
        video.with_suffix(".caption.txt").write_text(caption + "\n")
        ledger.update(status="captioned", caption=caption, caption_source=source, tagline=tag)
        print("caption:\n" + caption + "\n")

        # 3. host it where Meta can cURL it: S3 -> CloudFront (RADIO §8.4, D6)
        key = ig.reel_object_key(cfg, item, sha256_file(video))
        if deps.s3.head(key) != size:
            deps.s3.put_file(key, video, content_type=ig.VIDEO_CONTENT_TYPE,
                             cache_control=CACHE_CONTROL)
        url = cfg.media.public_base_url.rstrip("/") + "/" + key
        bad = ig.check_public_video(url, size, deps.head)
        if bad:
            raise RuntimeError("video is not publicly fetchable: " + "; ".join(bad))
        ledger.update(status="uploaded", video_key=key, video_url=url)
        print(f"video: {video} ({size} bytes)\nurl:   {url}")
    except Exception as e:  # noqa: BLE001 - report to the agent, never a traceback
        stage = ledger.data.get("status")
        ledger.update(status="failed", failed_at=stage, error=f"{type(e).__name__}: {e}"[:500])
        print(f"FAILED at {stage}: {e}", file=sys.stderr)
        _result("failed", item_id=args.item, stage=stage, error=str(e)[:300])
        return EXIT_FAILED

    if not args.publish:
        print("prepared; nothing was sent to Instagram (pass --publish to post)")
        _result("prepared", item_id=args.item, title=title, artist=artist, caption=caption,
                video=str(video), url=url, ledger=str(ledger.path))
        return EXIT_OK

    # 4. publish: the only outward action (INSTAGRAM §0.4)
    assert deps.graph is not None
    g = deps.graph
    record: dict[str, Any] = {}
    try:
        limit = await g.publishing_limit()
        if limit["quota_total"] and limit["quota_usage"] >= limit["quota_total"]:
            raise ig.GraphError(f"publishing quota used up ({limit['quota_usage']}/"
                                f"{limit['quota_total']} per 24 h)")
        cid = await g.create_reel_container(video_url=url, caption=caption)
        ledger.update(status="container", container_id=cid, quota=limit)
        await g.wait_until_finished(cid)
        mid = await g.publish(cid)
        # Published is published (D11): record the id before any further call can fail.
        posted_at = _now()
        ledger.update(status="published", media_id=mid, posted_at=posted_at)
        record = {"media_id": mid, "container_id": cid, "video_key": key,
                  "posted_at": posted_at, "permalink": None, "caption_source": source}
        ig.record_posted(store, args.item, record)
        media = await g.media(mid)
        record["permalink"] = media.get("permalink")
        ledger.update(permalink=record["permalink"],
                      media_product_type=media.get("media_product_type"))
        ig.record_posted(store, args.item, record)
    except ig.GraphError as e:
        stage = ledger.data.get("status")
        err = f"GraphError: {e} (code {e.code}, subcode {e.subcode})"[:500]
        if stage == "published":
            ledger.update(error=err)
            print(f"posted (media {record['media_id']}) but the permalink read failed: {e}",
                  file=sys.stderr)
            _result("published", item_id=args.item, title=title, artist=artist,
                    media_id=record["media_id"], permalink=None, caption=caption,
                    warning=str(e)[:300], ledger=str(ledger.path))
            return EXIT_OK
        ledger.update(status="failed", failed_at=stage, error=err)
        print(f"FAILED at {stage}: {e}", file=sys.stderr)
        _result("failed", item_id=args.item, stage=stage, error=str(e)[:300], code=e.code)
        return EXIT_FAILED

    print(f"posted: {record['permalink']}  (media {record['media_id']})")
    _result("published", item_id=args.item, title=title, artist=artist,
            media_id=record["media_id"], permalink=record["permalink"], caption=caption,
            ledger=str(ledger.path))
    return EXIT_OK


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", metavar="TEXT",
                    help="list songs whose title or artist contains TEXT, then exit")
    ap.add_argument("--item", type=int, help="song item id (from --find)")
    ap.add_argument("--publish", action="store_true", help="actually post to Instagram")
    ap.add_argument("--note", help="operator guidance for the caption line")
    ap.add_argument("--caption", help="use this caption line verbatim (no LLM call)")
    ap.add_argument("--force", action="store_true", help="repost a song already on the account")
    ap.add_argument("--start", type=float, default=0.0, help="clip start, seconds")
    ap.add_argument("--duration", type=float, default=None,
                    help="clip length, seconds (default instagram.clip_s)")
    args = ap.parse_args(argv)
    if bool(args.find) == (args.item is not None):
        ap.error("pass exactly one of --find TEXT or --item ID")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = load_config()
    store = open_store(cfg)  # live engine: RDS when DATABASE_URL is set, else SQLite
    try:
        if args.find:
            print_matches(find(store, args.find), args.find)
            raise SystemExit(EXIT_OK)
        graph: ig.GraphClient | None = None
        if args.publish:
            token, account = ig.load_instagram_env()
            if not token or not account:
                print(f"{ig.TOKEN_ENV} and {ig.ACCOUNT_ENV} must be set (.env)", file=sys.stderr)
                raise SystemExit(EXIT_USAGE)
            graph = ig.GraphClient(cfg, token, account)
        llm = None if args.caption else LLM(cfg, api_key=load_api_key())
        deps = Deps(s3=S3ObjectStore(cfg), llm=llm, graph=graph)

        async def go() -> int:
            try:
                return await run(cfg, store, args, deps)
            finally:
                if graph is not None:
                    await graph.close()
                if llm is not None:
                    await llm.close()

        raise SystemExit(asyncio.run(go()))
    finally:
        store.close()


if __name__ == "__main__":
    main()
