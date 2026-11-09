# SFX — MOSS-SoundEffect v2.0: probe, design, and prompting

> **Merged from the former SFX.md and SFX_PROMPTING.md during the docs
> reorganisation.** §1–§10 are the probe and design notes; §11 is the prompt
> guide.
>
> **2026-10-01: the SFX backend is now MOSS-SoundEffect v2.0 on `:8000`**
> (`docs/backends.md` §7, docs/models.md §4). The probe findings below describe the
> old Stable Audio model; the design (§3 onward) still applies.

Investigation of the local SFX model on port **8500** and how Pilgrim Dot Farm
should drop silly sound effects into its non-serious bits. Probe code + audio
samples live in `sfx_probe/`. **Status: implemented (2026-10-01)** — §8 first
cut is wired in: `pipelines/sfx.py`, `sfx_plan.py`, field reporter, sidecar
overlays in `/api/station/program`, client overlay playback, sim assertions.
RADIO.md §4/§5.2/§6.1/§9.2/§10/§11/§12/§15 carry the normative spec; this file
stays as the findings + rationale. What still needs a human is in §10.

---

## 0. The policy (how we'll use SFX on air)

These are the listening-side rules we settled on (they constrain the engineering
below, not the other way around).

- **Never with news.** News is the one serious, sober segment. SFX stays out of
  it, full stop. (Don't even wire the toggle; keep it structurally impossible.)
- **0–3 SFX per 10 seconds of air.** A rolling rate budget. Most segments
  (2–5 s) carry **0–1** stingers; a long talk-up might reach 2–3. This is a
  throttle, not a quota — sparse reads funnier than a wall of them.
- **Jokes get a 50/50 "da-dum-tiss".** When the DJ (or field reporter) lands a
  self-aware punchline, flip a coin (seeded RNG, see §7): half the time drop a
  rimshot `ba dum tss` in the beat, half the time leave it dry. The uneven
  delivery is the charm.
- **Timing will be uncanny anyway — don't chase perfection.** We are not
  building sample-accurate syllable placement. We aim at an *intentional beat*
  (a TTS pause, a phrase boundary) and accept a little drift. "Good enough and
  weird" is the target; over-polish is wasted effort.
- **Two on-air characters** drive contextual SFX: the full-time DJ (**Liam**)
  and a **field reporter** (new — §4).

---

## 1. The backend

| | |
|---|---|
| Endpoint | `http://127.0.0.1:8500` |
| Model id | `stable-audio-3-small-sfx` (stabilityai) |
| Device | `cpu` (reported by `/health`) |
| API style | OpenAI-compatible wrapper; only Real Paths = `/health`, `/v1/models`, `/v1/audio/speech` |
| Auth | none observed (local tunnel) |

- `GET /health` → `{"status":"ok","model":"stable-audio-3-small-sfx","device":"cpu"}`
- `GET /v1/models` → lists the model.
- Generation: `POST /v1/audio/speech` (one-shot, no streaming observed).

The HF repo is **gated**, so the model card can't be read; everything here is
from live probes.

### `POST /v1/audio/speech` — request schema (observed)

```jsonc
{
  "model": "stable-audio-3-small-sfx",
  "input": "a single slow cartoon cow moo, barnyard",   // REQUIRED prompt
  "voice": null,          // accepted, unused for SFX
  "response_format": "wav",  // wav | mp3 (others silently fall back to wav)
  "speed": null,
  "duration": 2.0,        // 0.5 .. 380.0; honored EXACTLY
  "negative_prompt": null,
  "steps": 8,             // default; more = sharper but slower
  "cfg_scale": 1.0,       // default; higher = more "opinionated"/pronounced
  "seed": null            // fixed -> fully deterministic
}
```

### Response (observed)

- `wav` → `audio/wav`, **PCM s16le, 44.1 kHz, 2ch stereo**, length == requested
  `duration`. Peak is frequently **full scale (1.0)**, RMS ~0.05–0.38.
- `mp3` → `audio/mpeg` (real). `flac`/`ogg` are ignored — server returns wav.
  → Standardize on **`wav`** (its own delivery lane, distinct from 24 kHz Kokoro).

---

## 2. How it ticks — measured behavior (CPU box, single request, warm)

| requested dur | wall time | notes |
|---|---|---|
| 0.5 s | ~1.9 s | cramped, RMS ~0.9; too short to read as a "hit" |
| 1 s | ~2.6 s | usable |
| 2 s | ~2.7–3.2 s | **sweet spot** for a stinger |
| 5 s | ~3.0 s | plateau — same as 2 s |
| 10 s | ~13 s | ~1.3× real time beyond ~5 s |

Takeaway: **~2–3 s fixed floor, then ≤~1.5× real time.** A 2 s stinger costs
~3 s wall. Fast enough to produce into inventory ahead of airing — **never at
playout** (AGENTS rule 1).

- `steps` 8→16 adds ~1.8 s for a 2 s clip. Keep `steps=8`; the difference is
  mostly inaudible for a comic hit.
- `cfg_scale` 1→7 raises RMS 0.13→0.24 (**more pronounced**). **cfg ≈ 3–5** reads
  crispest for cartoon onomatopoeia. Default 1.0 often lands a little soft.
- Fixed `seed` → **byte-identical WAV**. Reproduce / re-roll freely.
- Prompt style that worked: name the sound, mark it *cartoon/comic/comical*, give
  the onomatopoeia, optionally one scene noun (`barnyard`), < ~15 words.
- 1.5–2.5 s is the sweet spot for a punctual hit; >3 s drifts toward a bed.

> ⚠️ 12 curated samples are saved in **`sfx_probe/samples/`** — **listen to
> them**. SFX selection / voice assignment are human calls (AGENTS §9).

---

## 3. Step zero — survey the content, map the SFX

Before wiring anything, map **what we already produce → which SFX could serve it**.
Here's the current inventory of on-air content and its SFX prospects (target
`sfx_pool` column):

| Content type | Fresh / shared? | SFX prospects (`pool`) |
|---|---|---|
| `dj_talk` | fresh, never recycled | barnyard gags, wind/grass bed, joke rimshots |
| `field_report` *(new)* | fresh, never recycled | farm-prop stingers (sprinkler, animals), "outstanding in the field" beat |
| `commercial` | evergreen, recycled | product gag stingers (pig oink on "hogs"), silly-DJ hits |
| `liner` | evergreen, recycled | silly-DJ stingers (airhorn on sign-off) |
| `song` + `intro` | evergreen | rare; maybe a single intro whoosh, keep minimal |
| `news` | fresh | **none** — hard off (policy §0) |

This table (and the per-type examples in §5) is the concrete "what we have →
what SFX" deliverable.

---

## 4. Characters & persona (expanded cast)

The station currently has one full-time personality (**Liam**, `voice.md`).
We're adding context so SFX has someone to bounce off.

### 4.1 Liam — house DJ (existing)
Warm, wry late-night DJ. Owns the silly-DJ stinger vocabulary (rimshot,
airhorn, record scratch, sad trombone). Tell jokes with a beat for the 50/50
rimshot.

### 4.2 ***[Field Reporter]* (new)** — the literal field guy, from the UK
- A second voice role, produced by the same voice pipeline with a **new
  persona prompt** (`prompts/field.md`, sibling to `voice.md`).
- He is **British** (Kokoro male voice). **Voice chosen: `bm_lewis`** (user pick,
  sampled in `sfx_probe/voices/bm_lewis.wav`). Distinct from the American DJ
  `am_liam`. Config (when implemented): `voices.field_reporter: bm_lewis`.
- His whole bit: he's a **field reporter standing in a literal field** and he is
  **outstanding in the field** — the pun is the recurring gag, and it **must
  slip in** each time. His **catch phrase** (closing line, every report):
  > "This is **[NAME]** signing off with a reminder that I'm outstanding in
  > my field."
  (Name TBD by the user; placeholder `Giles` was used for the voice samples.)
- He delivers **farm factoids** as straight "news" that's really filler, e.g.:
  > "…and word from the south forty: **Farmer Jenkins is getting a new
  > irrigation system** — a center-pivot, if you can believe it."
- Roll this into a new contextual item type **`field_report`** (treated like
  `dj_talk`: fresh, never recycled, its own `inventory.field_reports_min`).

### 4.3 Banter shape (DJ ↔ field)
The DJ hands off to the reporter in the field; the reporter files farm facts;
DJ pops a stinger. This handoff is where the comedy (and most SFX) lives.

---

## 5. SFX usage examples (the "how do we actually use it" canon)

Concrete scripts with the intended SFX + timing/odds. These are the spec for the
selector + prompts.

**Example A — joke / 50/50 da-dum-tiss (`dj_talk`)**
```
DJ: "…I'd tell you a joke about my tractor, but it's a bit haywire."
[JOKE BEAT — pause]
SFX: rimshot "ba dum tss"  — 50% (seeded coin), else 0 (dry).
```

**Example B — field reporter, the catch phrase (`field_report`)**
```
[Reporter]: "…and that's the word from the south forty. This is [NAME]
signing off with a reminder that I'm outstanding in my field."
[PUN BEAT — catch phrase]
SFX: low wind/grass bed under the whole report (he's outside in a field);
     catch phrase itself stays dry or takes a single quiet hit — taste call.
```

**Example C — farm factoid (`field_report`)**
```
Chet: "Over at Jenkins' place the new center-pivot is soaking the north forty."
SFX: sprinkler psst-psst / gentle water spray on "soaking" (offset at the phrase).
```

**Example D — barnyard talk-up (`dj_talk`)**
```
DJ: "The roosters trounced the porch light again last night."
SFX: rooster crow (contextual, matches the topic).
```

**Example E — commercial gag (`commercial`, evergreen)**
```
VO: "Pilgrim Dot Farm feed — your hogs will dance."
SFX: pig oink on "hogs".
```

**Example F — sign-off liner (`liner`, evergreen)**
```
VO: "This is Pilgrim Dot Farm — stay weird."
SFX: airhorn (low gain, on "weird").
```

**No-SFX canon:** any `news` script — zero stingers, structurally excluded.

### Timing rule (the "uncanny is fine" part)
Place SFX at an **intentional beat**: the TTS join gap if known, a phrase
boundary, or (failing that) a coarse mid-point of the host — whichever is
cheapest. Do **not** build syllable-accurate placement. If `offset_s + sfxDur >
hostDur`, skip the stinger (never push a join).

---

## 6. The two pools (contextual + evergreen) — updated

1. **Barnyard / farm-prop stack** (contextual, fresh). Made per talk-up /
   field-report to fit the moment. **Confirmed keepers** (human-listened):
   **chicken cluck, cow moo, horse neigh, pig oink** — plus **sprinkler/
   irrigation water** and a **wind/grass bed** for the field reporter (not yet
   rendered). Never recycled (rule 10).
2. **Generic silly DJ shit** (evergreen stock, recycled like commercials/liners).
   Small pool made once at seed time. **Confirmed keeper so far: slide whistle**
   — the others (airhorn, rimshot, record scratch, sad trombone, boing,
   applause) were **"way off"** and need re-rolling/curation before they join.
   Cheap: ~3 s per clip.

> **Prompt lottery is real.** Only ~5 of the first 12 prompts gave a usable,
> on-brand hit. Treat the pool as *curated by ear*, not generated-and-trusted.
> Re-roll losers with a different `seed`/wording; keep what lands.

---

## 7. Recommended integration (respects the hard rules)

### 7.1 Sidecar hit, not a baked mix
Short SFX **overlay** the host clip (client-side) rather than being mixed in
server-side. New item type **`sfx`** in the existing `items` table (same schema,
no DB change). A host's `meta` records an optional sidecar:

```json
{ "type": "dj_talk", ..., "sfx": { "item_id": 4041, "offset_s": 1.7,
  "gain": 0.5, "pool": "barnyard", "coin": 1 } }
```

- `sfx` items are stored/served via `/api/media/{id}` but are **not airable
  solo** — only through a host's sidecar.
- Exposing the sidecar in `/api/station/program` is an **HTTP-API change → record
  in RADIO.md §11 in the same change** (AGENTS rule 8).

### 7.2 Selector — the policy lives here
- **No `random`/`time` in selector** (AGENTS rule 3): the 50/50 coin and the
  stinger choice use the injected **RNG + Clock**, so `make sim` is byte-stable.
- Enforce **0–3 per 10 s** as a rolling budget across committed air (count
  recent stingers within the last 10 s of program; allow ≤3).
- News hosts never even consult the SFX path (policy §0).
- Only attach a stinger when a host has an **intentional beat** (joke, pun,
  topical phrase, or TTS gap). Otherwise the host airs clean.

### 7.3 Producer (holds "generate on the fly" without blocking)
- New `sfx.py` pipeline: `prompt → POST /v1/audio/speech (wav, ~2 s, cfg≈4,
  seed) → QC (peak/LUFS) → normalize → store as sfx`.
- **Evergreen silly-DJ** pool generated at seed time (`seed.py`).
- **Contextual** barnyard/farm stinger generated when its host (dj_talk /
  field_report) is produced — same inventory/lookahead that absorbs DJ talk.
- **Never awaited at air time** (rule 1). Latency (≤~5 s) is someone-else's
  problem because it happens ahead of the air date.

### 7.4 Client — the actual "concurrent play" (Web Audio, §9)
The client already schedules each decoded `AudioBufferSourceNode` at an absolute
`ctx` time (`nextWhen`). A stinger is a second source started at
`host._when + sfx.offset_s`:

```js
function scheduleWithSfx(i, offset, token) {
  const src = scheduleOne(i, offset, token);          // host as today
  const it = items[i];
  if (it.sfx && it.sfx.buffer) {
    const s = ctx.createBufferSource(); s.buffer = it.sfx.buffer;
    const g = ctx.createGain(); g.gain.value = it.sfx.gain ?? 0.5;
    s.connect(g); g.connect(analyser);
    s.start(src._when, 0);                             // within the host window
  }
}
```

- Sources are `AnalyserNode`-wired before `destination`, so stingers ride the
  same meter / OFF-AIR guard.
- **Clipping is the one real trap**: model output is full-scale, two full-scale
  sources sum to +6 dBFS. → a stinger `GainNode` and/or a short host-duck
  (`GainNode` `setValueAtTime`) around the stinger window. Keep SFX ≤ 2.5 s and
  `offset + drift` inside the host (skip otherwise).
- **Stinger gain: TBD (user hasn't decided).** Leave it as a config knob
  (`meta.sfx.gain`, default ~0.5) and tune by ear once listening. It's not
  baked in, so we can adjust without regenerating clips.
- A missing/expired stinger is a **no-op never a stall** (rule 1): host still
  airs, sidecar is optional in the client.

### 7.5 Tests / fakes
- Add `fake_sfx` in `pilgrim/tests/fakes/` (deterministic short WAV / mock of
  `sfx.py`) so unit/sim/e2e never touch the real box (rule 2).
- `make sim` with seeded RNG asserts: **≤3 SFX per 10 s**, **no SFX on news**,
  **~50% rimshot after a marked joke**, and hosts-with-sfx stay gapless even
  when the stinger is missing.

---

## 8. Concrete first cut

1. `seed.py`: generate the evergreen silly-DJ pool (cfg 4, seed fixed, 1.5–2.5 s)
   → normalize → store `type='sfx'`, `meta.pool='evergreen'`.
2. Add **field reporter**: config voices/model, `prompts/field.md`, item type
   `field_report`, producer fill to `field_reports_min`. Voice = human TBD.
3. DJ + field scripts open intentional beats (joke / pun / topic phrase).
4. Producer attaches `meta.sfx.{item_id, offset_s, gain}` at those beats.
5. Server exposes the sidecar in `/api/station/program` (RADIO.md §11).
6. Client: `scheduleWithSfx` + gain staging / optional duck.
7. `make sim` (`fake_sfx`, seeded RNG) asserts the policy (§7.5).

---

## 9. Findings worth remembering

- **Deterministic** with `seed` — regenerate / re-roll freely.
- **Duration honored exactly** (0.5–380 s) — safe placement math.
- **wav = PCM s16le 44.1 kHz stereo** — separate lane from 24 kHz Kokoro.
- **~3 s floor, ≤~1.5× real time** — inventory, never playout.
- **Full-scale output** → always gain-stage when layering.
- `cfg≈3–5` reads crispest for comic hits; `steps=8` is fine.
- HF repo is gated; this local wrapper is the only spec. Re-probe if it changes.

---

## 10. Open questions (need human input)

**Where things stand after implementation:**
- Only ear-approved cues air: `cow`, `pig`, `horse`, `chicken` (contextual,
  fresh RNG seed per clip) and `slide_whistle` (evergreen, seed 24). Everything
  else is in `config.yaml` with `approved: false`.
- **The 50/50 rimshot is wired but silent until a rimshot is approved.**
- **The field reporter's wind bed is wired but silent until `wind_bed` is
  approved.** Same for `sprinkler` and `rooster`.
- Candidates to audition: `sfx_probe/rerolls/*.wav` (prompts + seeds in
  `sfx_probe/rerolls.log`). To approve one: copy its prompt into the cue, set
  its `seed` (pins that exact clip; drop the seed for contextual cues if you
  want variety), set `approved: true`.


1. **Field reporter voice — DONE ✅ `bm_lewis`.** British male, sampled at
   `sfx_probe/voices/bm_lewis.wav`. The voice gate (AGENTS §9) is cleared.
   Remaining: pick his **name** (`[NAME]` in the catch phrase; placeholder
   `Giles`), then wire into `config.voices.field_reporter` + RADIO.md §12 when
   implementing.
2. **Silly-DJ pool is thin (slide whistle only)** — re-roll airhorn/rimshot/
   scratch/trombone/boing/applause with different seeds/wording to expand it;
   chicken/cow/horse/pig are confirmed keepers.
3. **Stinger gain** — undecided; keep as config knob (~0.5 default), tune by
   ear once live.
4. **Field reporter cadence** — how often a `field_report` airs; the catch
   phrase closes every one (confirmed), the "outstanding" gag otherwise may
   vary.
5. **`[sfx: …]` beats in LLM scripts vs. independent placement** — a prompt tweak
   to `voice.md`/`field.md` to open explicit beats could improve timing, but it's
   a design choice. (Keep out of `news.md` regardless.)
6. **RADIO.md §11 API + §12 config changes** ride in with the implementing
   milestone.


---

## 11. Prompting the SFX model

<!-- Merged from the former docs/systems/sfx.md during the docs reorganisation. The probe findings in §1 describe the old Stable Audio model; the prompt guidance below is model-agnostic and still applies to MOSS-SoundEffect v2.0 (docs/backends.md §7). -->


Treat `stable-audio-3-small-sfx` as a sound-effect generator, not a general
language model and not an image generator.

### Prompt structure

Describe, in roughly this order:

1. **Sound source**
   - What physically makes the sound?
   - Examples: wooden door, gravel footsteps, electric motor, thunder,
     pneumatic valve, glass bottle, sci-fi energy weapon.

2. **Action**
   - What is the source doing?
   - Use concrete verbs: slams, scrapes, rattles, accelerates, impacts,
     hisses, cracks, powers down, rolls away.

3. **Temporal behavior**
   - Describe how the sound unfolds when useful.
   - Examples:
     - sharp attack and fast decay
     - slow mechanical startup followed by steady hum
     - three heavy impacts
     - rising whine followed by an abrupt stop
     - distant approach, pass-by, then fade into the distance

4. **Acoustic character**
   - Describe qualities that can actually be heard.
   - Examples:
     - heavy low-frequency body
     - metallic resonance
     - dry and crisp
     - muffled
     - distorted
     - deep sub-bass
     - bright transient
     - long reverberant tail

5. **Recording/environment**
   - Add this when it matters.
   - Examples:
     - close-miked
     - recorded outdoors
     - large concrete warehouse
     - small dead room
     - distant microphone perspective
     - cavernous reverb
     - vintage microphone character

A useful generic template is:

    TrackType: SFX, [source] [action], [temporal behavior],
    [acoustic character], [recording/environment].

`TrackType: SFX` is optional, but use it by default because it helps explicitly
tell the model that the requested output is a sound effect.

### Examples

    TrackType: SFX, heavy wooden warehouse door slamming shut,
    hard initial impact with a short rattling decay, deep low-mid body,
    close-miked in a large concrete room.

    TrackType: SFX, footsteps walking slowly across loose gravel,
    individual crunchy footfalls with small stones shifting after each step,
    natural outdoor recording, close microphone perspective.

    TrackType: SFX, large industrial electric motor powering on,
    initial relay click followed by a rising mechanical whine that settles
    into a deep steady hum, subtle metallic vibration, recorded indoors.

    TrackType: SFX, futuristic plasma weapon firing once,
    rapid electrical charge-up followed by a sharp energetic blast,
    deep bass impact, bright electrical crackle and a short synthetic echo.

    TrackType: SFX, ceramic coffee mug dropped onto a tile floor,
    single hard impact followed by several small broken pieces bouncing
    and settling, dry close-miked recording.

### Prefer literal audible descriptions

Describe what should be HEARD.

Good:

    heavy steel hatch slams shut with a resonant metallic boom

Weak:

    terrifying hatch closing on an abandoned spaceship

The second prompt mostly describes visual/story information. Translate the
idea into audible properties instead:

    TrackType: SFX, massive steel hatch slamming shut,
    heavy metallic impact followed by a deep resonant ring,
    mechanical latch clunks into place, large reverberant interior.

### Think like a sound-effects library

The model was trained substantially from AudioSparx and Freesound audio and
metadata. Prompts resembling useful descriptions of recorded sounds are
therefore a good default.

Prefer:

    diesel truck engine idling unevenly, close exterior recording,
    deep mechanical rumble and occasional metallic vibration

over:

    I need the sound of an old truck that a character has just discovered
    after surviving for twenty years in a post-apocalyptic wasteland.

Convert narrative requirements into acoustic requirements.

### Duration

Choose duration independently from the text prompt and make it appropriate
for the sound.

Examples:

- gunshot / impact / UI sound: ~1-3 seconds
- door, mechanical action, short sequence: ~2-6 seconds
- engine start or pass-by: ~5-10 seconds
- ambience: longer as necessary

Do not request ten seconds for a naturally half-second transient unless the
desired result actually includes a tail, aftermath, repetition, or ambience.

The model's generation API takes duration separately, e.g.:

    prompt="chugging train coming into station with horn"
    duration=7

### Complexity

Prefer one coherent acoustic event per generation.

If several events belong naturally together, describe their order explicitly:

    car approaches rapidly on wet pavement, passes close to the microphone,
    tire spray and engine roar peak during the pass, then fade into the distance

Avoid stuffing unrelated events into one prompt. Generate separate effects
and combine them later when precise timing or layering matters.

### Prompt refinement

If the result is wrong, do not merely make the prompt longer.

Identify what is wrong and strengthen the corresponding audible property.

Too soft:
    "hard violent impact, strong low-frequency transient"

Too reverberant:
    "dry close-miked recording in a dead room"

Too synthetic:
    "natural field recording, realistic acoustic sound"

Missing sequence:
    explicitly state the order:
    "short hiss, followed by a metallic click, then a heavy mechanical clunk"

Too busy:
    remove secondary sounds and request only the primary event.

### Rule of thumb

Build prompts as:

    WHAT MAKES THE SOUND
    + WHAT IT DOES
    + HOW IT CHANGES OVER TIME
    + WHAT IT SOUNDS LIKE
    + HOW/WHERE IT IS RECORDED

Be concrete, acoustic, and concise. Add detail when the detail corresponds
to something that could actually be heard.
