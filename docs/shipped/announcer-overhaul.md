# Announcer overhaul — discourse, clock accuracy, self-promotion

> *Formerly `ANNOUNCER_OVERHAUL_PROPOSITION.md` (cited as `OVERHAUL`).*

**Status: shipped 2026-11-01.**

A diagnosis of the station's between-song talk (variety, clock accuracy,
self-promotion) and the overhaul that followed. Normative behaviour: RADIO
§4, §5.2, §5.3, §6.1, §6.3, §10, §11, §12, §15.

Code: `pilgrim/producer/` (DJ talk, time checks, promos),
`pilgrim/pipelines/voice.py`, `pilgrim/pipelines/clocktime.py`,
`pilgrim/promotions.py`, `pilgrim/prompts/voice.md`, `promo.md`,
`pilgrim/bible/`. The diagnoses (§1–§4), the full proposition (§5) and the
proposed config (§6) are in the git copy; the review revisions below
superseded parts of them and are what shipped.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/shipped/announcer-overhaul.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

## Review revisions (2026-11-01, operator + review) — supersede the body

Where this section and the body below disagree, this section wins.

1. **Greetings.** Drop a greeting only when the *same voice* spoke in the
   preceding non-song item. A new voice (news reader, field reporter) keeps
   its greeting, filled from the air time so it is never wrong. Station-name
   mentions are normal radio; never suppress them.
2. **Clock: per-bucket variants picked at commit (replaces A3's predicted
   tail).** The LLM writes the copy once with `[[clock]]` and no time. The
   producer renders 2–3 variants, one per upcoming 5-minute bucket ("about
   ten past six", "about quarter past six", …; Kokoro ~0.2 s each). At
   commit the scheduler picks the variant whose bucket matches
   `_slot_wall()` of the slot — known exactly then. On a request splice that
   moves the slot, swap to the matching variant (the splice already bumps the
   revision). Error = the 5-minute rounding only. The time MCP's "now" is the
   wrong input: audio is committed 300 s ahead and sits in stock first.
   The template stays on the shelf until a variant is committed; only the
   cheap renders are discarded.
3. **A1 stands** (expiry against the air slot; expiry-aware `build_state`
   count). Stamp `expires_at` after the LLM call, not before.
4. **Newest first.** `_pick_fresh_talk` (and promos) take the newest
   unexpired clip, not `rng.choice`. Consider newest-first for new
   commercials too.
5. **Word bans scoped.** Relative day words and spelled-out clock times are
   banned only in DJ talk, time checks and promos, and only when they are not
   the code-supplied phrase. News and commercials are exempt (news has its own
   TTL); `tts_cleanup` keeps rewriting times for commercials.
6. **Day-level facts** ("Sunday night", "the first of November", campaign
   timing) are computed in code from the station timezone, used verbatim, and
   the clip expires at the next local midnight.
7. **`time_check` text is code-written** (a few variants around `[[clock]]`),
   no LLM.
8. **Requests are already announced** (intro spliced before every request
   song; 327/327 aired requests consumed theirs). The 0-of-1,881 figure counts
   DJ breaks only. Add: a just-aired request is required context for the next
   DJ break (back-announce); a ready-but-waiting request gets a "coming up"
   tease; the request-line plug may cite real recent counts. Never a name or
   email.
9. **B3 airtime-weight conversion: dropped.** Reading the current weights as
   airtime shares would cut songs to ~55% and triple commercials. Keep the
   soft talk floor and nudge `dj_talk` by hand.
10. **B2 motifs:** a config list of rationed motifs (sprinkler, barn, "we
    just", "welcome back") with a minimum gap, string-matched. No NLP.
11. **B5 intros:** per-song named intros from code-written variants ("Here's
    [[title]] from [[artist]]"), rendered once per song, no LLM, recycled like
    liners (RADIO §4 must class them evergreen).
12. **C6 admin promo endpoints: deferred.**
13. **Target spread per hour:** songs ~49 min (~24); commercials ~3.5 min;
    DJ breaks 6 × 18 s; named song intros ~14 × 5 s; request intros as
    fulfilled; time checks 4 × 4 s (one at :00); IDs/liners 6 × 5 s; news
    2 × 19 s; field 2 × 18 s; promos 1–2 × 12 s. ≈ 6 min voice/h, songs ~80%.

    > **Amended 2026-11-01 (reviewer, same day):** the arithmetic above is
    > wrong in two places. **Songs are the remainder, not a target:**
    > `60 − ~6.1 voice − 3.5 spots = 50.4 min ≈ 27 songs` at the measured
    > 110.3 s. And **DJ breaks should not drop:** 6 × 18 s is 1.8 min/h
    > against a measured baseline of 2.6 min/h (11.7 breaks/h × 13.4 s).
    > Start at **8 × 20 s** (parity, tunable), with 10 × 20 s (+27%) the
    > option if more Liam is wanted. See B0.

### Operator decisions (2026-11-01) — final

1. **DJ spot count: unchanged.** No count target in `hour_budget`; the
   selector's existing weight keeps producing ~11.7 breaks/h. `target_s` stays
   18. (Airtime parity with today.)
2. **Second host: approved.** American, woman. Voice id is still a listening
   decision (AGENTS §9) — candidates rendered for the operator's ear; config
   ships the chosen id, with a provisional default clearly marked.
3. **Liners: `liners_per_bucket: 4`** (20 liners), not 5.
4. **Thanksgiving angles: approved.** We are pandering; lean in.
5. **No Thanksgiving song steering.** Stock songs stay general; listener
   requests for Thanksgiving songs are fulfilled normally.
6. **Time checks: 4/h at :00, :15, :30, :45.** Not "roughly hourly" — the
   draw targets those quarter marks within a window.
7. **Go.** Implement in the revision block's order.

### Thanksgiving (replaces C1–C5 where they differ)

- Light touch: the audience is small. Timing is **optional**; most promos
  just say Thanksgiving is coming to Pilgrim Dot Farm.
- When timing is mentioned, code supplies the colloquial phrase from days to
  2026-11-26 (station tz), used verbatim; the clip expires at local midnight:

  | Days out | Phrase |
  |---|---|
  | 2+ | the exact count: "nine days away" |
  | 1 | "tomorrow" |
  | 0 | "Happy Thanksgiving" |

  (Changed 2026-11-17, operator decision: the earlier vague buckets said "a
  couple weeks away" at nine days out.)

- No spoken calendar date; no `require_date_every_n`.
- Ramp: ~1 promo/h, 2/h in the final week, a few extra on the day (gravity
  rule respected after news). Every promo names "Pilgrim Dot Farm" and
  "Thanksgiving" (code-checked).
- On the day: turkey-census field report, Thanksgiving DJ topics, "a song for
  the table" request plug. Campaign off after 11/26; leftovers retired.
- Decoupled from the clock templates: ships right after A1 so the ramp starts
  on time.
- Open: optionally steer 6–10 station songs (`prompts/station_song.md`)
  toward Thanksgiving — operator decision.

### Order

1. A1 + newest-first + expiry stamped after the LLM call.
2. Campaign config, `promo` type, promo prompt + validator, request-line plug.
3. `time_check` (code text); stop passing the clock to DJ talk.
4. Request back-announce / tease context; B1 rotation; B4 liners; B2 motifs.
5. B5 named intros.
6. Clock variants for any DJ copy that still wants `[[clock]]`.

## 0. The three verdicts

1. **Variety is thin — but the problem is structural, not literal.** The LLM
   writes 1,880 unique texts, so nothing repeats word for word. What repeats is
   the *recipe*: every clip gets the same two ingredients (the last three songs
   + the clock), the same 18 s target, the same shape (station name → time →
   sprinkler/barn → back-announce), and the same motifs. 51% of the last run's
   DJ breaks mentioned the sprinkler; 37% said "we just"; 0 of 1,881 ever
   mentioned the request line; only 6.6% looked forward at all.
2. **The clock is right; the airing is wrong.** `spoken_time()` is
   timezone-correct (`America/Detroit`) and the phrasing is good. But the time
   is frozen at *production* and only checked at *commit*, so clips air a
   **median of 12 minutes late**, 66% more than 10 minutes late, worst 25
   minutes. Nearly half of all DJ airings landed after their own
   `expires_at`. Every single DJ break also reads the clock, which no real
   station does. News says "good morning" at noon in 29% of airings; field
   reports do the same in 30%. **The fix is cheap because Kokoro is fast:**
   let the LLM write a *template* with a `[[clock]]` token; the producer renders
   one cheap variant per upcoming 5-minute bucket (~0.2 s each) and the
   scheduler commits the variant whose bucket matches the actual air slot.
   Measured on the live box, Kokoro renders 16.25 s of DJ copy in **0.22 s** —
   so the slow LLM leaves the critical path and the clock becomes accurate to
   the rounding.
3. **There is no self-promotion at all.** The only station ID brief asks for
   the name plus "you're tuned in". There is no promo content type, no campaign
   calendar, no request-line plug, no "coming up" tease, and nothing in the
   code or config knows that **Thanksgiving, 2026-11-26** exists. Today the
   station airs **zero** promos per hour with 25 days of runway.

## 8. What I would NOT do

- **No audio splicing.** Pre-rendering the segments around the clock and
  stitching a fresh clock in would save one Kokoro call; Kokoro needs 0.22 s
  for a whole DJ break, so the stitching (seams, per-piece QC, three renders)
  is not worth it. Re-render the whole clip at commit.
- **No second DJ voice.** Voice assignment is a human listening decision
  (AGENTS §9). Variety comes from copy, topics, and structure, not from a
  second Kokoro voice — see Q2.
- **No new backend or API.** Weather comes from config phrases; topics from a
  config bank; dates from config. Nothing new to probe (AGENTS §2).
- **No LLM-authored dates.** The event date is a config value that the copy
  must reproduce verbatim.
- **No free-form real-world promotion.** The station's fiction stays intact;
  the campaign is station lore, not a real-world claim.
- **No change to the HTTP API or DB schema without the RADIO.md update in the
  same change** (AGENTS rule 8).
- **No touching `pilgrim/payments.py`.** Promo copy mentions the request line;
  it never touches money, never imports Stripe (AGENTS rule 11).

---

## 9. Decisions (all answered by the operator, 2026-11-01)

- **Q1 — DJ breaks: unchanged.** No count target; the selector's weight keeps
  ~11.7/h. `target_s` stays 18.
- **Q2 — Second host: shipped.** American, woman; shift-based
  (`talk.hosts`, midday/afternoon/evening). **The voice id is still yours by
  ear** — `af_bella` is provisional. Samples:
  `voice-samples/af_bella|af_nicole|af_sarah|af_river.wav`. Change one line in
  `config.yaml` to switch.
- **Q3 — Paid-priority copy: not requested.** The request-line plug is plain
  self-promotion; no payment language.
- **Q4 — Liners: `liners_per_bucket: 4`** (20 total).
- **Q5 — Thanksgiving angles: approved, pandering encouraged.**
  Ramp starts **2026-11-01** (operator: "start it today, I want to hear it").
- **Q8 — Paid-tier promotion: steady, folded into the normal promo mix.**
  `paid_tier` every 30 min, Tree Fiddy Report hourly in `am_justin`, request
  line every 45 min. Site untouched.
- **Q6 — No Thanksgiving song steering.** Requests are fulfilled normally.
- **Q7 — Clock reads: 4/h at :00/:15/:30/:45.** Shipped; the sim shows
  97/day = 4.04/h, each speaking its own 5-minute bucket.

---

## 10. How we will know it worked

Re-run the §2/§3 queries after rollout and compare:

| Metric | Today | Target |
|---|---|---|
| DJ breaks per hour | 11.7 at 13.4 s (2.6 min/h) | 8 at 20 s (2.7 min/h) |
| Median clock error at air | 12 min | ≤ 2.5 min (5-min rounding only) |
| Share of DJ airings after `expires_at` | 49% | 0% |
| DJ clips produced but never aired | 43% | ~0% (templates don't expire) |
| Share of breaks mentioning "sprinkler" | 51% | < 15% |
| Max plays of one liner per 4 h | 6 | ≤ 2 |
| Songs aired with an intro | 1 of 125 | ≥ 14/h named (revision 11) |
| News greeting contradicts air time | 29% | 0% |
| Request-line mentions | 0 of 1,881 | ≥ 1 per 45 min |
| Promos per hour, final week | 0 | 2 (revision block) |
| Promos on 11/26 (Thanksgiving) | 0 | a few extra, after non-serious bulletins |
