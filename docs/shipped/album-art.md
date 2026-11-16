# Album art — cover art for every song

> *Formerly `ALBUM_ART.md`.*

**Status: shipped** (Steps 1–11, 2026-11-02).

Every song gets a 1024×1024 cover with its title and artist written on it,
made by the local FLUX 3 model, uploaded to S3 and shown in the RADIO
BROADCAST pane. Normative behaviour: RADIO §8.4 (media), §9.1 (client), §11
(API), §14 (storage).

Code: `pilgrim/album_art.py` (`ArtWorker`), `pilgrim/pipelines/art.py`
(`SceneWriter`, FLUX client), `pilgrim/prompts/album_art_scene.md`,
`album_art_flux.md`, `pilgrim/tools/art_backfill.py`, `art_smoke.py`,
`make_reel.py`. Backend shapes: docs/backends.md. Reels built on this are in
docs/shipped/instagram.md.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/album-art.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.2 Rules that follow from those facts (non-negotiable)

1. **Always `response_format: "b64_json"`. Never use the returned `url` or
   `/download`.** That URL serves the most recent render. Any other caller
   between our render and our download gives us their image, which would
   put the wrong cover on a song.
2. **One image at a time, station-wide.** The art worker has exactly one
   in-flight request. No `n > 1`.
3. **Respect memory.** The M5 is also a song generator (mlx-serve). Before
   each render, read free memory the same way `serve.py` does (`vm_stat`:
   free + inactive + "occupied by compressor" pages × page size) and skip if it is below
   `art.min_free_gb`. A 503 `memory_pressure` is a normal back-off, not an
   error: wait `art.retry_after_s` and try again later. Never retry in a
   tight loop.
4. **Timeouts:** `/health` can take ~40 s while a render runs, so the health
   probe timeout is ≥ 60 s and a slow health check is not "down". The
   generation call timeout is `art.timeout_s` (default 180 s).
5. **Art never touches playout** (hard rule 1). A song with no art airs
   exactly as today. Nothing in `scheduler.py`, `selector.py` or
   `producer.py` imports the art code or waits on it.
6. **Real renders only in `make art-smoke` (when asked) and the backfill
   tool run by the operator.** Tests use `pilgrim/tests/fakes/fake_flux.py`
   (hard rule 2).

### 0.3 The "album" problem, and the prompt that fixed it

If you put "album cover", "album art", "record" or "LP" in the prompt, FLUX
draws a **vinyl record / circle** instead of square artwork. Two test renders
with the template below gave clean, full-bleed square art with **exactly
correct** title and artist text, including `Production? That's Just a
Label.` (punctuation and apostrophe intact). The only defect was gibberish
pseudo-text on props in the scene (box labels, a screen). So the scene must
not contain things that carry writing.

**Final FLUX prompt = fixed template (in code-owned `prompts/album_art_flux.md`)
+ one LLM-written scene.** The title and artist strings are inserted by
code, **never** by the LLM:

```
Square 1:1 graphic artwork that fills the entire frame edge to edge, flat and
full-bleed, no border, no frame, no vinyl record, no disc, no circle, no CD,
no mockup, no product photo. {scene} Every person depicted is a clearly adult
man or woman; no children, no teenagers. Large clear typography at the top
reads exactly "{title}". Smaller text at the bottom reads exactly
"{artist}". No other text, no signs, no labels, no logos.
```

**Adults only.** Every person in a cover must be a clearly adult man or
woman. The fixed template states this, the scene prompt repeats it, and
`validate_scene` rejects any scene containing a minor-age word (`child`,
`children`, `kid`, `kids`, `baby`, `babies`, `toddler`, `infant`, `teen`,
`teenager`, `teenage`, `adolescent`, `boy`, `boys`, `girl`, `girls`,
`schoolboy`, `schoolgirl`, `schoolchildren`, `preteen`, `pre-teens`, `youth`,
`youths`; whole-word, case-insensitive) and asks the LLM again. This applies
regardless of the listener request: a premise about "daughters", students or
siblings is rendered with unmistakably adult figures.

Banned words in the scene and the template: `album`, `cover`, `record`,
`vinyl`, `LP`, `disc`, `CD`, `single`, `sleeve`, `cassette`. Enforce this in
code: reject the scene and ask the LLM again if any appear (word-boundary,
case-insensitive), the same way `songs.py` validates briefs.

Text sanitising before inserting into the template: strip emoji and control
characters, collapse whitespace, and replace `"` with `'`. For the art
text only, drop a trailing parenthetical such as `(Piano Version)` first.
Then cap title at 70 and artist at 48 characters, truncating at a word
boundary. The DB title is never changed. If the title or artist contains
any **non-Latin-script** character (Japanese, Tamil, …; accented Latin like
Spanish is fine), render that song **text-free**: replace the two
typography sentences with `No text, no letters, no typography.` If either field
is empty, skip the song (no art), because the label rule in RADIO §9.1 needs
both fields too.

## Decisions (defaults chosen; operator may override)

- **D1 — Concurrency with song generation.** Default: art renders run
  alongside M5 song renders, gated only by `min_free_gb` and one-at-a-time.
  (Measured: two 40 s renders completed with mlx-serve resident and ~36 GB
  free.) Alternative: a config flag `art.yield_to_local_song: true` that
  waits while the M5 (`mlx-serve`) pool backend holds a lease, which costs
  art throughput but leaves the GPU to the song.
- **D2 — Text accuracy.** Default: trust FLUX's text (2/2 exact in tests),
  with no OCR check. Misspelled art can be fixed with
  `art_backfill.py --item ID --force --seed N`. Alternative: render text-free
  art and overlay title/artist with ffmpeg `drawtext` (always spelled
  right, looks less integrated).
- **D3 — Non-song segments / songs without art.** Default: show a static
  station placeholder image (`web/img/`; operator to supply, or Step 8 adds
  a plain retro "PILGRIM DOT FARM" tile).
- **D4 — Reel audio window.** Default: first 60 s. We don't have chorus
  timestamps.

## Answers to the agent's pre-flight questions (2026-11-02)

The step text above is already updated for 1–5 and 9.

1. Config: `hosts.flux`, not `endpoints.flux`.
2. Health: public `/api/health` gets only the cached `backend` status;
   counts go on admin (option b).
3. Scene input: fall back to the caption/style_prompt (Step 3). Caption-only
   is fine for songs with no lyrics.
4. Caps: drop a trailing parenthetical, then 70/48 at a word boundary. The
   DB is unchanged.
5. Non-Latin titles/artists: render text-free. Accented Latin renders
   normally.
6. D1 stands: no yield flag now. Run alongside, gated by `min_free_gb`,
   one at a time.
7. `art.enabled: true`. The worker chews through the backlog (upcoming
   songs first, then newest). The backfill tool stays for targeted
   re-renders (`--item`, `--force`).
8. Add a plain retro "PILGRIM DOT FARM" placeholder tile in `web/img/`. The
   frame always shows the placeholder (never hides) for non-songs, so PLAY
   never moves.
9. 300 px square.
10. Yes: point AGENTS.md's active work plan at this file (docs/shipped/payments.md
    paused). Commits cite the ALBUM_ART step **and** the RADIO sections
    touched (AGENTS still requires RADIO citations).
11. Confirmed: (a) request/paid songs get art like any song; (b) no origin
    art fallback, so `""` until uploaded, and a micro-cached program may
    briefly lack it; (c) reels only on demand, from S3 copies, never posted.
    The small choices you listed (key shape, column types, `library/art/`,
    suffixes, no OCR) are fine.
