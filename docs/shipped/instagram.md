# Instagram Reels — from aired songs, operator-triggered

> *Formerly `INSTAGRAM.md` at the repo root.*

**Status: shipped** (Steps 1–7, 2026-11-11). The agent runbook is
docs/instagram.md; observed Graph API shapes are docs/backends.md §14.

`make reel-post` finds an aired song, builds a square Reel from its cover and
MP3, writes a caption in the tagline voice, and publishes through the Meta
Graph API on the operator's word. The station process never posts. Normative
behaviour: RADIO §8.4, §12, §14.

Code: `pilgrim/instagram.py` (caption writer, Graph client, ledger),
`pilgrim/tools/post_reel.py`, `make_reel.py`,
`pilgrim/prompts/instagram_caption.md`, `pilgrim/bible/taglines.md`,
`pilgrim/tests/fakes/` (`FakeGraph`).

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:INSTAGRAM.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.2 Decisions in force (brief of 2026-11-08 + defaults)

| # | Decision | Why |
|---|----------|-----|
| D1 | **Two commands, one confirmation.** `--find TEXT` lists candidates (id, title, artist, length, postable?, posted?). The agent shows them and asks the operator to confirm **full title and artist** (and the id when titles repeat). Then `--item ID --publish` does everything. `--item ID` without `--publish` prepares (video, caption, upload) and stops, for inspection | The brief: "after confirming which song (full title and artist), agent publishes"; "do not publish without an explicit operator instruction" |
| D2 | **Video: 1024×1024 square, the art as-is, first 20 s, static.** `instagram.layout: square`, `instagram.clip_s: 20`. The existing 1080×1920 blurred-frame layout stays as `vertical` (and stays the `make reel` default) | The brief. Meta allows 1:1 (0.01:1–10:1) but recommends 9:16; a square Reel shows with blank bars in the Reels tab. Operator may flip `layout` to `vertical` any time (§10 Q1) |
| D3 | **Caption = one LLM line + deterministic tail.** `<line>\n\n“<Title>” by <Artist>. Request your own at https://pilgrim.farm — link in bio.\n\n#ai #aimusic #aimusicgenerator #pilgrimdotfarm`. The line is one or two short sentences (≤ 280 chars) in the voice of the tagline bank, about this song; the model is given the whole bank and may riff on one. URL, CTA, hashtags, song line come from config, never from the model | The brief ("more or less one of the one-liners", "hash tags and URL deterministically"); the operator's own 2026-11-08 post has this shape |
| D4 | **The line is validated deterministically and falls back to a verbatim tagline.** Rejected: hashtags, handles, URLs/domains, emoji, markdown, line breaks, > 3 sentences, restating the title, profanity, wrong length. After `instagram.caption_attempts` (3) rejections the line is `bank[item_id % len(bank)]`, verbatim. `--caption TEXT` uses the operator's line instead of the model; `--note TEXT` steers the model | AGENTS §5: LLM output is untrusted; a Reel post must never block on the LLM |
| D5 | **Profanity stays out of captions.** Tagline #16 is dropped from the bank handed to the model and from the fallback; a model line containing profanity is rejected | `style_guide.md`: profanity banned; a caption is station copy |
| D6 | **Host the MP4 on the existing bucket under `reels/<stem>-<sha256[:16]>.mp4`**, `Content-Type: video/mp4`, the same immutable cache header, and **HEAD it through CloudFront** (200, `video/mp4`, `accept-ranges: bytes`, matching length) before any Graph call | Meta cURLs `video_url`; the infra exists; content-addressed keys never change bytes behind the year-long cache (RADIO §8.4) |
| D7 | **Graph API: Facebook-Login flavour.** `hosts.graph = https://graph.facebook.com`, `instagram.api_version = v27.0`, token only in `Authorization: Bearer`, never in a URL, log line, ledger or caption | §0.1: the token is a system-user token; URLs end up in logs |
| D8 | **`share_to_feed: true`, `is_ai_generated: true`** on the container, both config knobs | The station is openly AI-generated (house voice); Meta offers the self-disclosure flag. Operator may turn either off (§10 Q2) |
| D9 | **Duplicate prevention in the `meta` table**: key `instagram:reel:<item_id>` → JSON `{media_id, container_id, video_key, posted_at, permalink, caption_source}`. A second run on the same song exits 3 (`already_posted`) unless `--force`. Also a per-run JSON ledger `pilgrim/logs/reels/<stem>.json` rewritten at every stage, plus `<stem>-sq.caption.txt` | No schema change (hard rule 8); `items.meta_json` is owned by the station process (art worker), so a tool must not read-modify-write it |
| D10 | **The quota is checked first** (`content_publishing_limit`); at `quota_usage >= quota_total` the run fails before creating a container | Observed 100/24 h; a refused publish after a container would waste the container |
| D11 | **"Published is published."** `media_id` is written to the ledger and the store the instant `media_publish` returns; if the permalink read then fails the run still reports `published` with `permalink: null` and a warning | The agent must never re-run `--publish` after a success it did not see |
| D12 | **Machine-readable last line.** Every run ends with `RESULT {json}` on stdout (`status` ∈ `prepared|published|already_posted|failed|usage`) | The caller is a local agent |

### 0.3 Non-goals (Phase 1)

No scheduler/queue, no automatic posting, no engagement ranking, no admin
UI, no web-tier endpoint, no Stories/carousels, no cover-photo upload, no
resumable (`rupload`) upload, no Instagram-Login token flow, no App Review, no
Terraform/AWS changes, no changes to `player.js`, `scheduler.py`,
`selector.py`, `producer.py`, `server.py` or `web_app.py`.

### 0.4 The Graph calls this plan makes (contract)

All on `https://graph.facebook.com/v27.0`, header
`Authorization: Bearer <INSTAGRAM_TOKEN>`, form-encoded bodies. `{ig}` =
`INSTAGRAM_ACCOUNT_ID` (`17841480340352745`).

| # | Call | Params | Success | Used for |
|---|------|--------|---------|----------|
| 1 | `GET /{ig}` | `fields=id,username,media_count` | `{id, username, media_count}` | smoke / sanity |
| 2 | `GET /{ig}/content_publishing_limit` | `fields=quota_usage,config` | `{"data":[{"quota_usage":n,"config":{"quota_total":100,"quota_duration":86400}}]}` | D10 |
| 3 | `POST /{ig}/media` | `media_type=REELS`, `video_url`, `caption`, `share_to_feed=true|false`, `is_ai_generated=true` (when `ai_label`) | `{"id": "<container>"}` | create the Reel container |
| 4 | `GET /{container}` | `fields=status_code,status` | `{"status_code": "IN_PROGRESS"|"FINISHED"|"ERROR"|"EXPIRED"|"PUBLISHED", "status": "…", "id": …}` | poll every `poll_s` up to `poll_timeout_s` |
| 5 | `POST /{ig}/media_publish` | `creation_id=<container>` | `{"id": "<media>"}` | publish |
| 6 | `GET /{media}` | `fields=id,permalink,media_type,media_product_type,timestamp` | `{id, permalink, media_type: "VIDEO", media_product_type: "REELS", timestamp}` | record the permalink |

Errors: HTTP 4xx with `{"error": {"message", "type", "code", "error_subcode"?, "fbtrace_id"}}`
→ `GraphError`, **not retried**. HTTP 5xx and transport errors → retried with
backoff up to `instagram.max_attempts`. Step 7 records the observed
`status_code` sequence and timings in `docs/backends.md` §14.

### 0.5 Files

| File | Step | What |
|------|------|------|
| `pilgrim/config.py`, `pilgrim/config.yaml` | 1 | `hosts.graph`, `models.instagram_caption`, `instagram:` block |
| `pilgrim/tests/conftest.py`, `README.md`, `RADIO.md` §12 | 1 | secrets blanked in tests; env documented; config sketch |
| `pilgrim/tools/make_reel.py`, `pilgrim/tests/test_art_tools.py` | 2 | `square` layout, spec flags, start-time check |
| `pilgrim/instagram.py` (new), `pilgrim/prompts/instagram_caption.md` (new) | 3, 4 | tagline bank, caption writer + validation + composer, Graph client, ledger, store record |
| `pilgrim/tests/fakes/fake_graph.py` (new), `pilgrim/tests/test_instagram.py` (new) | 3–5 | fakes-only tests |
| `pilgrim/tools/post_reel.py` (new), `Makefile` | 5 | the command; `make reel-post` |
| `docs/backends.md` §14, `docs/instagram.md` (new), `RADIO.md` §8.4 §14, `AGENTS.md` | 6 | observed shapes; agent runbook; spec |

## 8. Agent operating protocol (summary of docs/instagram.md)

```
operator: "I want <title> posted to reels"
agent:    make reel-post REEL_ARGS="--find '<title words>'"
          -> shows matches; asks "Post “<Title>” by <Artist> (id N)?"   (always asks)
operator: "yes" (+ optional caption guidance)
agent:    make reel-post REEL_ARGS="--item N --publish [--note '…' | --caption '…']"
          -> reports RESULT: permalink + caption, or stage + error
```

Rules the agent never breaks: no `--publish` without that yes; confirm by id
when titles repeat; a `POSTED` row needs a second explicit yes for `--force`;
`status: published, permalink: null` means live — do not retry; never print,
paste or log `INSTAGRAM_TOKEN`.

## 9. Acceptance checklist

- [ ] `make lint`, `make test`, `make sim`, `make e2e` green after every step.
- [ ] `make test` makes no network call: `FakeGraph`, `FakeS3`, fake HEAD/GET,
      `FakeLLM`/scripted LLM only; `INSTAGRAM_*` blanked by conftest.
- [ ] Square MP4: 1024×1024, h264 High, yuv420p, progressive, 30/1, both
      `start_time` 0, no `elst`, `ftyp`+`moov` first, AAC 48 kHz stereo 128k.
- [ ] Caption = LLM line (validated; fallback verbatim tagline) + `“Title” by
      Artist.` + CTA + the four hashtags; profanity never reaches a caption.
- [ ] Token only in the `Authorization` header (`FakeGraph.bad_auth == []`).
- [ ] `--item` without `--publish` makes zero Graph calls; `--publish` makes
      exactly: limit, media, N×status, media_publish, media.
- [ ] Second `--publish` on the same song exits 3 with no Graph calls; `--force` reposts.
- [ ] One real Reel posted in Step 7 with the operator's explicit yes; permalink
      recorded in the `meta` table and the ledger; backends.md §14 filled in.
- [ ] `scheduler.py`, `selector.py`, `producer.py`, `server.py`, `web_app.py`,
      `player.js` untouched (`git diff --stat` per step).

## 10. Open questions for the operator

1. **Square or vertical by default?** The brief says 1024×1024 square; Meta
   recommends 9:16 and will letterbox a 1:1 Reel in the Reels tab. The 9:16
   blurred-frame builder already exists (`instagram.layout: vertical`). Default
   here: square (D2).
2. **`is_ai_generated: true` on every post?** It adds Meta's "AI info" label.
   The station's voice is openly AI; default on (D8). `share_to_feed: true`
   also default on.
3. **Token lifetime.** The system-user token cannot be inspected without an app
   token. Please confirm in Business Settings → System users → Generate token /
   token info that it was created as "never expires"; if not, note the date
   and we add a reminder. (Do not paste the token anywhere.)
4. **Name the song in the caption?** `“Title” by Artist.` before the CTA
   (default on, D3); the brief said not to *restate* the title, which the LLM
   line obeys; this line is for viewers who want to find the song.
5. **Caption review before posting?** Today `--publish` posts the LLM line
   unseen (the agent confirms the *song*, not the caption). If you want to see
   the caption first, the agent runs `--item N` (prepare), shows the caption,
   then `--item N --publish --caption '<the shown line>'`. Say which you want
   as the default protocol and docs/instagram.md will say so.
6. **Hashtag set.** `#ai #aimusic #aimusicgenerator #pilgrimdotfarm` per the
   brief; the 2026-11-08 feed post used `#ai #localllm #selfhostedai`. Config
   knob `instagram.hashtags`.
