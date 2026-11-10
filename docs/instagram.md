# Posting a song to Instagram Reels (INSTAGRAM.md)

Who runs this: the operator, or the local agent **on the operator's word**.
Nothing here is automatic. The station process never posts.

## The conversation

1. Operator: "I want *[song title]* posted to Reels" (optionally with caption
   guidance: "mention the banjo", or a dictated line).
2. Agent runs
   `make reel-post REEL_ARGS="--find '<a few words of the title>'"`
   and reads the list: `id  Title — Artist  (m:ss)  [not postable …]  POSTED <permalink>`.
   - 0 matches: say so; try a shorter query or the artist name; never guess an id.
   - 1+ matches: show them and ask the operator to confirm **the full title and
     the artist** (and the id when a title appears twice). Always ask, even with
     one match.
   - A `POSTED` row is already on the account: say so and ask whether to repost
     (`--force`) before doing anything.
3. Operator confirms → agent runs
   `make reel-post REEL_ARGS="--item <id> --publish"`, adding
   `--note '<guidance>'` when the operator gave caption guidance, or
   `--caption '<line>'` when the operator dictated the line.
4. Agent reports the last line's `RESULT` to the operator: the permalink and the
   caption as posted (`status: published`), or the failure stage and error
   (`status: failed`). If `status: published` with `permalink: null`, the Reel
   **is live**; the permalink read failed; do not run `--publish` again.

Without the operator's confirmation in step 2 the agent may run `--find` and
`--item <id>` (prepare only: it writes `pilgrim/logs/reels/<stem>-sq.mp4`,
`<stem>-sq.caption.txt`, `<stem>-sq.json` and uploads the MP4 to S3) but
**never `--publish`**.

## What `--item <id> --publish` does

build the 20 s 1024×1024 clip from the CloudFront copies → one LLM call for the
caption line (voice: `bible/taglines.md`; falls back to a verbatim tagline) →
append `“Title” by Artist. Request your own at https://pilgrim.farm — link in
bio.` and `#ai #aimusic #aimusicgenerator #pilgrimdotfarm` → upload
`reels/<stem>-<sha>.mp4` to S3, HEAD it through CloudFront → check the
publishing quota → create the Reel container → poll until FINISHED → publish →
read the permalink → record it (`meta` table key `instagram:reel:<id>`, and the
ledger JSON).

## Exit codes / RESULT.status

| exit | status | meaning |
|------|--------|---------|
| 0 | `prepared` | built, captioned, uploaded; nothing sent to Instagram |
| 0 | `published` | live; `permalink` (may be null with a `warning`) |
| 1 | `failed` | `stage` says where (`building`, `built`, `captioned`, `uploaded`, `container`); nothing is live |
| 2 | `usage` | no such song / not postable / bad flags |
| 3 | `already_posted` | `prior` has the earlier `permalink`; use `--force` to repost |

## Operator knobs (`pilgrim/config.yaml` → `instagram:`)

`layout` (square | vertical), `clip_s`, `share_to_feed`, `ai_label`, `cta`,
`hashtags`, `name_the_song`, `caption_attempts`; the model is
`models.instagram_caption`. Secrets: `INSTAGRAM_TOKEN`, `INSTAGRAM_ACCOUNT_ID`
in the origin `.env` (README "Secrets").

## When it fails

- `container ERROR: …` — Meta rejected the file. Check `<stem>-sq.json`,
  `ffprobe` the MP4, compare docs/backends.md §14 spec. Containers are not
  billed against the quota.
- `video is not publicly fetchable` — the CloudFront HEAD failed; check the
  `mac-studio` AWS profile and `media.public_base_url`.
- `OAuthException code 190` — the token is invalid/expired; the operator
  regenerates it in Business Settings → System users (never paste it in chat).
- `publishing quota used up` — 100 posts per 24 h on the account; wait.
