# UI refactor — app shell: ABOUT pane, desktop nav, phone tabs + mini player

> *Formerly `UI_REFACTOR.md`, `UI_REFACTOR_FEEDBACK.md`.*

**Status: shipped 2026-11-08** (Steps 1–6 and four operator feedback rounds).

The site became an app shell: an ABOUT pane, a desktop section nav with an
active highlight, and on phones bottom tabs showing one pane at a time over a
mini player. The feedback rounds added the paid flow's promo step and
enumerated wait states, the queue pager, the sparkling cornucopia and the
phone type sizes that stop iOS zooming. Normative behaviour: RADIO §9.1, §9.2.

Code: `pilgrim/web/index.html`, `style.css`, `shell.js`, `titlebar.js`,
`pay.js`, `requests.js`; `pilgrim/tests/e2e/`. The round-by-round feedback log
(`docs/plans/ui-feedback.md`) is in git at the same commit; its one deferred
item is in PENDING.md.

> Condensed 2026-11-16. The step-by-step plan (exact code, test edits, deploy
> handoff) is in git: `git show c712f27:docs/plans/ui-refactor.md`. Section numbers cited in code
> comments and commits refer to that file; the sections kept below keep their
> original numbers.

### 0.1 What we are building, in one paragraph

Today the page is one vertical stack of seven retro "window" panes inside a
scrolling `#desktop`, under a header and above a footer. After this plan the
**desktop** (≥ 900 px wide) looks the same, except that the last four panes
(HIT COUNTER & SOCIAL, CORPORATE MISSION STATEMENT, INFRA AND DATA FLOW,
CREDITS) live inside one **ABOUT** pane and a small fixed **left nav**
(RADIO / REQUEST LINE / SHEEP PEN / ABOUT) scrolls to a pane and highlights
the one in view. On a **phone** (≤ 899 px) the page becomes an app: a
one-line header (hat + station name, no tagline), **one pane at a time**
filling the screen, a **mini player** (dot, song title, PLAY/STOP) and four
**bottom tabs** (RADIO / REQUEST / CHAT / ABOUT); the footer disappears and
its copyright line moves to the bottom of ABOUT. Playback is untouched: the
mini player is a mirror of the existing PLAY button and indicator.

### 0.2 Facts about the client you must not get wrong

- **There is no HLS and no `<audio>` chain.** Playback is Web Audio
  (`decodeAudioData` + `AudioBufferSourceNode.start(when)`) with one
  persistent `#sink` element as the output device (RADIO §9.2, AGENTS hard
  rule 9). An earlier draft of this plan spoke of "the audio element or HLS
  instance"; that language was wrong for this site. Nothing in this plan
  creates, moves, clones or replaces `#sink`, `#btn-play`, `#indicator`,
  `#level` or the `AudioContext`.
- **`player.js` is not edited in this plan.** Not one line. The mini player
  reads state that already exists: `#btn-play` gains class `stop` while
  playing and is `disabled` while OFF AIR; `#indicator` carries class `idle`,
  `buffering` or `onair`; `document` receives `pd:nowplaying` with
  `{title, artist, …}` for the audible song or `null` (RADIO §9.2). The mini
  PLAY button calls `document.getElementById("btn-play").click()` from inside
  its own click handler, so the `AudioContext` is still created inside a user
  gesture (AGENTS §10 "AudioContext must be created inside the PLAY click
  handler").
- **The shell already exists.** `body` is `height:100%; overflow:hidden;
  display:flex; flex-direction:column`; `#titlebar` and `#footer` are
  ordinary flex children and `#desktop` is the one scroll container
  (`flex:1; min-height:0; overflow-y:auto`). Nothing is `position:fixed`
  today. Keep it that way on the phone too: the mini player and tab row are
  flex children between `#desktop` and the bottom of `body`, never fixed
  elements (fixed elements float around when iOS opens the keyboard).
- **`#desktop.has-more + #footer::before`** (style.css) is the bottom fade
  cue, toggled by titlebar.js. The adjacency `#desktop + #footer` must stay,
  so the new mini player and tab row are inserted **after** `#footer` in the
  DOM and ordered visually by CSS (the footer is `display:none` on phones).
- **`titlebar.js`** already sizes the hat to the logo's rendered height on
  every resize; the one-line phone logo gets a correctly scaled hat for free.
- **Tests run in `make e2e`** (Playwright, `pilgrim/tests/e2e/`, against the
  fakes-only `e2e_server.py`). The default project is Desktop Chrome at
  1280×720; a few tests set 320–480 px viewports explicitly and will fall into
  phone mode after Step 3. No Playwright MCP, no real browser session against
  production. Screenshots, when you want to look at something, go to the
  /tmp/ui-refactor-screens/ (D18), never into the repo.
- **Cache busting** is the `?v=` query on each `<link>`/`<script>` in
  `index.html`. Bump it on every file you change (date-stamped,
  e.g. `?v=20261109a`).

### 0.3 Decisions in force (operator, 2026-11-08 + defaults)

| # | Decision | Why |
|---|----------|-----|
| D1 | **One phone, not a device matrix.** Automated phone tests use Playwright's `iPhone 14` descriptor (390×664 CSS px, 3×, mobile UA, touch) in Chromium. Real iPhone Safari + one Android are the operator's manual check after deploy | "Assume just one standard sized phone" |
| D2 | **Two modes, one breakpoint: 899/900 px.** ≤ 899 px is the phone shell; ≥ 900 px is the desktop with the left nav. No third "tablet" layout. **Landscape phones stay in the phone shell** (844×390 is ≤ 899 px; RADIO scrolls there, the intended fallback) | Simplest thing that keeps desktop unchanged; 900 px is where a 170 px nav + 24 px gap first fits beside the 484 px column with 16 px to spare |
| D3 | **Phone header = hat + `P I L G R I M DOT FARM (BETA)` on one line, no tagline.** Logo font size is `clamp(12px, 4.2vw, 19px)` so the line always fits; titlebar.js sizes the hat to it. **The hat is exactly the text line's height** (answered 2026-11-08) | Header and footer were eating phone height |
| D4 | **Footer hidden on phones; `© <year> Pilgrim Dot Farm LLC` becomes the last line of ABOUT.** Desktop footer unchanged | Operator request |
| D5 | **Inactive phone panes are `visibility:hidden`, not `display:none`.** All four panes stay laid out, stacked with `position:absolute; inset:0` inside `#desktop`; only `.active` is visible | Scroll positions, the chat log's follow-the-bottom logic, form text and the queue's scroll cue all keep working while a pane is hidden, with no save/restore code |
| D6 | **Keyboard: the shell is sized by `visualViewport.height`** (CSS var `--vvh`), and while a text field has focus and the keyboard has taken ≥ 120 px, the tab row and mini player hide (`body.kbd-open`) so the composer sits directly above the keys. Viewport meta gains `viewport-fit=cover, interactive-widget=resizes-content` | The composer must not be covered; native apps hide the tab bar under the keyboard. **Needs a real-phone check (operator)** |
| D7 | **Mini player = mirror, zero audio code.** A dot (idle/buffering/on air), the audible song as `Title — Artist` (or `PILGRIM DOT FARM` / `BUFFERING…` / `ON AIR` / `OFF THE AIR`), and a round PLAY/STOP that clicks `#btn-play`. Tapping the title area switches to RADIO without touching playback | AGENTS hard rules 1 and 9; RADIO §9.2 |
| D8 | **RADIO fits the phone without scrolling** by shrinking only the cover art: `.art-frame` width = `min(var(--art-w), max(140px, calc(var(--vvh) - 440px)))`. The rate row, play row and meter keep `min(100%, 300px)`. If an unusually short viewport still overflows, the pane body scrolls | 390×664 budget in §0.5; controls are never clipped |
| D9 | **Desktop nav is `position:fixed` in the left margin**, 170 px wide, right edge 24 px left of the column; the column's width and centring do not change by a pixel | Operator: "desktop basically the same ... and then a nav pane" |
| D10 | **Active-section highlight** = the last pane whose top is above the line 35 % down `#desktop`'s viewport; at the bottom of the scroll, ABOUT. rAF-throttled scroll listener, no IntersectionObserver | Deterministic, no flicker, trivial to test |
| D11 | **Tab persistence**: the selected phone tab is kept in `sessionStorage` `pd_tab` (default `radio`), so a reload returns to the same tab | Cheap; matches the paid-draft convention (`pd_pay_draft`) |
| D12 | **ABOUT sub-sections keep the exact former titles** in this order: HIT COUNTER & SOCIAL, CORPORATE MISSION STATEMENT, INFRA AND DATA FLOW, CREDITS, each `section.about-sub[data-sub=…]` with an `h2.about-head`. Their bodies are moved, not retyped | Tests and RADIO §9.1 name these |
| D13 | **The agent deploys (operator override, 2026-11-08).** The agent builds and pushes the image, bumps `ecs_release`, applies Terraform and vets the running container with the AWS CLI, sparingly (one deploy at the end). Local verification needs no push | Operator request; supersedes the repo's "agent does not deploy" convention |
| D14 | **No pane title bars on phones** (answered 2026-11-08): `.win-bar` is `display:none` in the phone shell because the bottom tabs already name the pane. The one exception is SHEEP PEN's bar, which survives as a thin right-aligned **status strip** (`#chat-status` only, the `SHEEP PEN` title hidden) because `chat.js` writes the connection state there. Desktop bars unchanged | ~28 px more for the art; the chat status must stay visible |
| D15 | **One deploy, at the end.** After Step 6's commit and all checks pass: build `linux/arm64`, push `pilgrimdotfarm/front-end:<sha>` and `:latest`, bump `ecs_release` 14→15, apply, vet. No intermediate deploys | Use the slow deploy path sparingly |
| D16 | **The operator's real-phone check comes after the agent's iPhone-14-in-Chromium check and the deploy.** No station restart is needed (no `pilgrim/*.py` change) | Operator, 2026-11-08 |
| D17 | **EC2 is gone and no nginx serves the front-end.** The Fargate web tier is the only web surface; there is no edge checkout to `git pull` | Operator, 2026-11-08 |
| D18 | **Screenshots go to `/tmp/ui-refactor-screens/`**, never into the repo | Operator, 2026-11-08 |

### 0.4 Non-goals

No change to `pilgrim/*.py`, the HTTP API, config, DB, nginx templates,
Dockerfile, Terraform, `player.js`, `chat.js`, `rating.js`, `requests.js`,
`pay.js`, `votes.js`, `voter.js`, `visitors.js`. No framework, no build
step, no new dependency. No change to the desktop radio pane's look. No new
`<audio>`/`<video>` elements. No change to any pane's content or copy other
than the ABOUT wrapper and the copyright line.

### 0.5 Phone height budget (why D8's numbers are what they are)

At 390×664 (Playwright iPhone 14): header ≈ 36 px (one 16.4 px line + 8 px
padding ×2 + border), no RADIO title bar (D14), radio body fixed content
≈ 281 px (padding 20, rate row 64, play row 44, meter 22, song label 41,
download row 28, status 14, six 8 px gaps), mini player 49 px, tab row 53 px.
That leaves 664 − 419 = **245 px** for the square art; `calc(664px − 440px)`
= 224 px, ≈ 20 px of slack for font metrics. A real iPhone 14 in Safari
(≈ 714 px visible) gets 274 px art. An iPhone SE (≈ 553 px visible) hits the
140 px floor and the body scrolls by a few px, which is the intended
fallback. A landscape phone (≈ 390 px visible) scrolls the RADIO body by
about 170 px; that is the fallback too (D2).

### 0.6 Selector glossary (use these names; invent none)

| Thing | Selector |
|-------|----------|
| Scroll container / phone pane host | `#desktop` |
| Panes | `.window[data-win="radio"|"requests"|"sheep"|"about"]` (direct children of `#desktop`) |
| ABOUT sub-sections | `.about-sub[data-sub="hit"|"motivation"|"infra"|"credits"]`, heading `.about-head` |
| Desktop nav | `#nav-desktop button[data-go=…]` |
| Phone tabs | `#nav-mobile button[data-go=…]` |
| Mini player | `#mini`, `#mini-title` (button, `data-go="radio"`), `#mini-text`, `#mini-play` |
| Copyright | `#footer .copyright` (desktop), `.about-copyright` (phone), year spans `.year` |
| Active states | `aria-current="page"` on the nav/tab button; `.window.active` on the phone pane |
| Shell JS | `pilgrim/web/shell.js` (new, loaded last) |
| Mode | CSS `@media (max-width: 899px)` / JS `matchMedia("(max-width: 899px)")` |

## 8. Open questions for the operator

All answered by the operator on 2026-11-08; the defaults in §0.3 are final.

1. **Hat size on phones (D3).** Answered: the hat is exactly the height of
   the one-line logo text. No `(BETA)` removal, no two-line wrap.
2. **Keyboard (D6).** Answered: hide the tabs and mini player while a text
   field has focus and the keyboard is up, as specified.
3. **Landscape phones (D2).** Answered: landscape keeps the phone shell;
   RADIO scrolling there is the accepted fallback.
4. **Phone title bars (D14).** Answered: none on phones, the bottom tabs
   name the pane. SHEEP PEN keeps only its status strip.

---

## 9. Shipped

Filled in by the agent at deploy time (D15; docs/shipped/sheep-pen.md convention).

- Date: 2026-11-08
- Commit / image tag: `526d3c9` → `pilgrimdotfarm/front-end:526d3c9` and `:latest`
- Digest: `sha256:42ca837f75ece2644d16c4dc0fcb1bcb7f101fbf142c5d443c7f73ace652bc5f`
- `ecs_release`: 14 → 15 (`pg-app:11`, ARM64, release label 15)
- Task definition: `pg-app:11`; service `pg-app` PRIMARY deployment `COMPLETED`,
  1/1 running, task `RUNNING`/`HEALTHY`; `https://pilgrim.farm/` serves
  `shell.js?v=20261109a` and the ABOUT pane
- Operator real-phone check (D16): pending (iPhone Safari + one Android Chrome;
  see Step 6.4)

**Feedback round (2026-11-08, docs/shipped/ui-refactor.md):** commit `4073bee`,
image `pilgrimdotfarm/front-end:4073bee` + `:latest`, digest
`sha256:c384a8e443ddaff3ca73fcc4ec40c4a1c9bffb1135f21005f62e9036efcc8808`,
`ecs_release` 15 → 16 (`pg-app:12`, HEALTHY), live site serves
`style.css?v=20261109b` / `shell.js?v=20261109b` / `requests.js?v=20261109b`
and `img/cornucopia.webp`.

**Feedback round 2 (2026-11-08):** commits `3492b4f` + `b688d4e`, image
`pilgrimdotfarm/front-end:b688d4e` + `:latest`, digest
`sha256:f5110a493291fe024d775f446f172380e66a70a1328d4e43f8d450af19d367de`,
`ecs_release` 17 → 18 (`pg-app:14`, HEALTHY). Paid flow: grey promo step with
APPLY (two attempts), enumerated (1/3)(2/3)(3/3) stages, both lyrics and music
required, Cancel removed, stale-token lock fixed. Live test passed
(request 587, paid, queued).

**Round 2 follow-ups (2026-11-08):** commits `5dac39e` + `0d802cc`, image
`pilgrimdotfarm/front-end:0d802cc` + `:latest`, digest
`sha256:635ab9b06355e2d49ed0cc5b0c50e3aebbc724cbd413bacdbd2b5720b62937fb`,
`ecs_release` 18 → 19 (`pg-app:15`, HEALTHY). Promo status line no longer
reserves a blank row; the phone queue box keeps its 520 px height (flex
children no longer shrink); ABOUT heads have no rule above, an inset rule
below and a uniform 14 px rhythm; the cornucopia credit is removed.

**Round 3 (2026-11-08):** commit `bfe58ba`, image
`pilgrimdotfarm/front-end:bfe58ba` + `:latest`, digest
`sha256:0051d210a405dc3f4ff5150f843ff4cd8e4d94ca3e1c96b93362790a286993e1`,
`ecs_release` 19 → 20 (`pg-app:16`, HEALTHY). ABOUT heads share the request
line's left indent (24 px, 16 px at ≤520), the mission image matches the rule
width, free-request controls use the site font, and phone form controls are
16 px so iOS does not zoom on focus.
