# Prompt upgrade — paid request input: three boxes (lyrics, music, cover art)

> *Formerly `PROMPT_UPGRADE.md`.*

**Status: shipped 2026-11-08** (Steps 1–6). Supersedes the single paid text
box in docs/shipped/payments.md.

The PAID REQUE$T panel has three boxes: lyrical direction, musical direction
and cover-art direction, each feeding the part of the pipeline it is for. A
later round added the optional PROMO CODE step and made lyrics and music both
required (docs/shipped/ui-refactor.md). Normative behaviour: RADIO §6.2, §8.4,
§9.1, §11, §12, §14.

Code: `pilgrim/pipelines/request_parts.py` (`RequestParts`),
`pilgrim/pipelines/songs.py` (`brief`), `pilgrim/pipelines/art.py`,
`pilgrim/prompts/paid_request.md`, `paid_summary.md`, `pilgrim/web/pay.js`.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/prompt-upgrade.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.1 What we are building, in one paragraph

Today `POST /api/paid-requests` takes `{text}`; the text is moderated,
screened for copied lyrics, summarised for the board, stored in
`requests.text`, and later handed whole to the song-brief LLM
(`pipelines/songs.py` `brief(request_text=…)`), the DJ intro and the cover
scene writer (`pipelines/art.py` `SceneWriter.write(request_text=…)`). After
this plan the panel shows three labelled boxes, the client posts
`{lyrics, music, art}`, the server stores the three parts in three new
columns **and** a labelled, composed copy in `requests.text` (so every
existing consumer keeps working), and the producer routes each part to the
pipeline it belongs to. The brief LLM receives the lyrics and music parts,
labelled, and never the art part. The scene writer receives the art part as
the primary brief, with the lyrics part as subject context. Nothing about
payment, moderation order, the queue, the board, or playout changes.

### 0.3 What the two prompting guides say that this plan uses

**MiniMax Music 3** (huggingface.co/spaces/multimodalart/minimax-music3-prompting-guide,
read 2026-11-07; matches `song_brief.md`):

- Two inputs, kept separate: **lyrics** (words + bracketed section tags) and
  the **structured caption** (all musical control). Lyric content never goes
  in the caption.
- Tags: `intro, verse, pre-chorus, chorus, post-chorus, bridge, instrumental,
  solo, outro`, **each on its own line**; words on a tag's line are dropped.
- Caption: three headings in order, `Global Metadata`, `Vocal Details`,
  `Arrangement`, about 250–450 words. **Vocal gender and timbre must be
  stated** ("the #1 cause of unwanted instrumental drift"); an instrumental is
  declared explicitly with a named lead instrument.
- Don't fabricate precision (BPM/key only when wanted); never contradict
  yourself; precedence: explicit requirements > section-tag directives >
  implications of the description > genre defaults.
- The requested duration is an upper bound: **the song ends when the lyrics
  run out**.

So the **lyrics box** is where words, subject, title/artist wishes and
section tags belong, and the **music box** is where genre, tempo, key, mood,
vocals, instruments and exclusions belong (or a whole pasted caption).

**FLUX** (docs.bfl.ml/guides/prompting_unified_basics, read 2026-11-07):

- Template, slots optional: subject, location, style/medium, lighting,
  colours, effects, extra elements. Medium/style first works well.
- Plain sentences that state relationships (who holds what, where the light
  comes from). Short prompts work; add words only for what you want to
  control.
- **No negative prompt**: describe what you want instead of what you don't.
- Leave out vague praise ("beautiful", "masterpiece"); describe only what a
  viewer could point to.
- Text goes in quotes with a stated position and type style. We already do
  this in code (`typography_sentence`); the listener must **not** ask for
  words on the cover.

So the **art box** helper text asks for subject, setting, medium, palette and
mood in positive terms, and the scene prompt is told to translate any "no X"
into what is there instead.

### 0.4 Decisions (defaults; the operator may override, see §8)

| # | Decision | Why |
|---|----------|-----|
| D1 | Three numbered boxes, in this order: **1 LYRICS** (*The words*), **2 MUSICAL DIRECTION** (*The sound*), **3 COVER ART** (*Optional*). Lyrics or musical direction is required; cover art is optional | Operator request + UX brief (§0.6). Art alone is not a song request |
| D2 | API: `POST /api/paid-requests` accepts `{lyrics, music, art}` (strings, each optional, at least one of lyrics/music non-empty). `{text}` stays accepted for one release; `text` alone behaves exactly as today | Cached `pay.js` on listeners' browsers must keep working during the rollout |
| D3 | Storage: three additive columns `lyrics_text`, `music_text`, `art_text` on `requests`; `text` keeps a **composed, labelled** copy (§1.1). Legacy and free rows leave the three columns NULL | `text` is read by the producer, intro, summary, moderation, admin and backfills; keeping it canonical means an old station process still works against new rows |
| D4 | Moderation runs once on the composed text (all three parts). Copyright screen and board summary run on lyrics + music only (never art) | One LLM call; art text is never sung, so it cannot be copied lyrics |
| D5 | Brief LLM gets two labelled parts (lyrics, music), never the art part. Style routing and the pasted-caption detector read the **music** box first, the lyrics box as fallback; the tagged-lyrics detector reads the **lyrics** box first | Stops subject words in the lyrics from pinning a style; keeps art direction out of the song |
| D6 | Scene writer gets the art box as the primary brief and the lyrics box as subject context; the comedic priorities in `album_art_scene.md` apply only when no art direction is given (or it asks for funny) | A payer who asks for an earnest cover gets one |
| D7 | Operator comp token: first line of the **lyrics** box (or of legacy `text`) | One rule, first box |
| D8 | Limits: `payments.parts.lyrics_max 4000`, `music_max 3000`, `art_max 800` characters; `paid_max_length` rises to 8000 for the composed text; `paid_max_lines` stays 200 | A full caption (~3000 chars) plus a full lyric sheet (~2500) plus art still fits; the MiniMax 4096-token budget is about what we *send* to MiniMax, which the brief validator already bounds |
| D9 | No change to the board, votes, Stripe flow, webhook, status route, queue order, intro wording, or the FLUX template | Scope |
| D10 | Existing paid rows are not back-split into parts | They aired or are in flight; the producer handles NULL parts |

### 0.6 UX brief (operator, 2026-11-07; governs Step 5)

We are selling **creative control**, not access to model parameters. The
listener is directing three departments: a songwriter, a music producer and
an album artist. They may direct any or all of them, as long as there is
enough to make a song. A submission of *Lyrics: "A raccoon becomes president
of the moon." / Musical direction: "Death metal." / Cover art: blank* must
feel complete and effortless; a pasted 5,000-character brief must feel
powerful, in the same form, with no mode switch.

Rules for the copy and layout:

- Field names on the page: **LYRICS**, **MUSICAL DIRECTION**, **COVER ART**,
  numbered 1–3, each with a two-or-three-word subtitle and one short helper
  line. Examples live in placeholders, not in prose.
- "Direct the lyrics", never "write the lyrics": the form must not imply the
  listener has to author anything.
- An empty cover-art box is framed as a choice ("Or let the farm surprise
  you"), not an incomplete form.
- No **LLM, prompt engineering, structured metadata, FLUX, MiniMax, token** or
  **caption** in the main form. Technical guidance (section tags, the full
  structured caption, vocal and arrangement detail, how to describe imagery)
  lives behind one optional expandable help block, where those words are
  allowed.
- Tone: playful, irreverent, confident, approachable. Good: *"Or let the farm
  surprise you."*, *"Your imagination. Our questionable machinery."* Avoid:
  *"Enter your structured musical generation metadata."*
- Payment area: button **PRIORITY REQUEST** (keep the `($3 MIN)` line, it is
  true), one explanation line *"Your request goes ahead of free
  submissions."*, CTA **CHECK IT & PAY**. Never imply immediate playback.
- Mobile-first, compact, the established neon palette and typography, a
  clear visual distinction between the three boxes, no unnecessary
  scrolling. Preserve unfinished drafts across a reload.
- Everything below the UI (API, storage, moderation, routing, payments,
  queue) is exactly what Steps 1–4 and 6 say; this brief changes copy and
  presentation only.

## 8. Open questions for the operator

Defaults are in §0.4. The agent builds the default unless told otherwise.

1. **Box names on the page.** **Answered 2026-11-07 (UX brief, §0.6):**
   `LYRICS / MUSICAL DIRECTION / COVER ART`, numbered, with the subtitles
   *The words / The sound / Optional*.
2. **Per-box limits** (D8): 4000 / 3000 / 800. Raise the art box if people
   paste long FLUX prompts?
3. **Comp token location** (D7): lyrics box first line only, or any box?
4. **Earnest covers** (D6): the scene prompt now defers to the listener's tone
   when art direction is given. Keep the comedic default for paid requests
   that give *no* art direction, or make paid covers earnest by default?
5. **Legacy `{text}` body** (D2): drop it after one release, or keep it as an
   API convenience indefinitely?
6. **Draft persistence.** **Answered 2026-11-07 (UX brief):** yes, in
   scope; Step 5.3 keeps drafts in `sessionStorage` under `pd_pay_draft`.
7. **The `($3 MIN)` line on the button.** The UX brief shows the price as
   `$3.50+`. The button keeps `($3 MIN)` because $3.00 is the real Stripe
   minimum; the in-panel terms line says *"$3.50 suggested, $3 minimum"*.
   Prefer `$3.50+` on the button instead?
