# UI_REFACTOR.md — App shell: ABOUT pane, desktop nav, phone tabs + mini player

Work plan for a coding agent. Six steps, done **in order**, one commit per
step. Every code block in this file is the exact code to write unless the
step says "adapt". Do not improvise beyond what a step says; if a step
cannot be done as written, stop and report (AGENTS.md §9).

Read this whole file first. Then read, in order: `AGENTS.md` (§1 hard rules
1, 9; §7; §8), `RADIO.md` §9.1 and §9.2, `pilgrim/web/index.html`,
`pilgrim/web/style.css`, `pilgrim/web/titlebar.js`, and the top 120 lines of
`pilgrim/web/player.js`. `RADIO.md` is the source of truth; if this file and
RADIO.md disagree, stop and report.

Each step ends with `make lint`, `make test`, `make e2e`, a commit whose
message cites the RADIO sections named in the step, and an AGENTS.md §8
report. Step 6 also runs `make sim` (AGENTS §7 in full). All work happens on
the Mac checkout at `/Users/justinherter/prod/pilgrim-dot-farm`. **Do not
restart the station, build or push an image, or touch AWS**: Step 6 ends by
handing the operator a deploy checklist.

---

## 0. Read this first

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

---

## Step 1. ABOUT pane + copyright line (RADIO §9.1)

### 1.1 `index.html`

Replace the four sections `data-win="hit"`, `"motivation"`, `"infra"`,
`"credits"` (everything from `<section class="window hit" …>` through the
closing `</section>` of credits) with **one** section. Move each former
`.win-body` block verbatim (cut and paste; do not retype the credits list or
the image `alt` texts):

```html
  <section class="window about" data-win="about">
    <div class="win-bar">
      <span class="win-title">ABOUT</span>
    </div>
    <div class="win-body about">
      <section class="about-sub" data-sub="hit">
        <h2 class="about-head">HIT COUNTER &amp; SOCIAL</h2>
        <!-- the former <div class="win-body hit"> … </div>, unchanged -->
      </section>
      <section class="about-sub" data-sub="motivation">
        <h2 class="about-head">CORPORATE MISSION STATEMENT</h2>
        <!-- the former <div class="win-body infra"> … </div> of the motivation pane, unchanged -->
      </section>
      <section class="about-sub" data-sub="infra">
        <h2 class="about-head">INFRA AND DATA FLOW</h2>
        <!-- the former <div class="win-body infra"> … </div> of the infra pane, unchanged -->
      </section>
      <section class="about-sub" data-sub="credits">
        <h2 class="about-head">CREDITS</h2>
        <!-- the former <div class="win-body credits"> … </div>, unchanged -->
      </section>
      <div class="about-copyright">© <span class="year"></span> Pilgrim Dot Farm LLC</div>
    </div>
  </section>
```

Footer: change `<span id="year"></span>` to `<span id="year" class="year"></span>`
and the inline year script to fill every `.year`:

```html
<script>
  document.querySelectorAll('.year').forEach(el => { el.textContent = new Date().getFullYear(); });
</script>
```

Bump `style.css?v=`.

### 1.2 `style.css`

Add directly after the `.window.infra …` / `.infra-link:hover …` block
(before the `#footer` rule):

```css
/* ABOUT (UI_REFACTOR.md Step 1, RADIO §9.1): one pane holding the hit
   counter, mission statement, infra diagram and credits as sub-sections with
   their former titles. The sub-section bodies keep their own .win-body.*
   rules; the outer body carries no padding so dividers run edge to edge. */
.window.about { width: min(94vw, 484px); }
.win-body.about { padding: 0; gap: 0; align-items: stretch; text-align: left; }
.about-sub + .about-sub { border-top: 1px solid var(--violet); }
.about-head {
  margin: 0; padding: 10px 14px; font-size: 14px; font-weight: bold;
  letter-spacing: 1px; color: var(--text); text-shadow: 0 0 6px var(--magenta);
  background: linear-gradient(90deg, rgba(255,47,214,.12), rgba(36,224,255,.06));
  border-bottom: 1px solid rgba(166, 61, 255, .5);
}
/* The phone shell shows this instead of #footer (Step 3). */
.about-copyright { display: none; }
```

The old `.window.credits`, `.window.hit`, `.window.infra` width rules may
stay (they no longer match anything); do not delete other rules.

### 1.3 Tests: `pilgrim/tests/e2e/cosmetic.spec.ts`

In the first test (`page layout: pane order…`):

```ts
  const order = await page.$$eval("#desktop > .window", els => els.map(e => e.getAttribute("data-win")));
  expect(order).toEqual(["radio", "requests", "sheep", "about"]);
  expect(await page.textContent('[data-win="about"] .win-title')).toBe("ABOUT");
  const subs = await page.$$eval(".about-sub", els => els.map(e => [e.getAttribute("data-sub"), e.querySelector(".about-head")!.textContent]));
  expect(subs).toEqual([
    ["hit", "HIT COUNTER & SOCIAL"],
    ["motivation", "CORPORATE MISSION STATEMENT"],
    ["infra", "INFRA AND DATA FLOW"],
    ["credits", "CREDITS"],
  ]);
```

and replace every remaining `[data-win="hit"]`, `[data-win="motivation"]
.win-title`, `[data-win="infra"] .win-title`, `[data-win="credits"]`,
`[data-win="credits"] .win-title` in that file with `[data-sub="hit"]`,
`[data-sub="motivation"] .about-head`, `[data-sub="infra"] .about-head`,
`[data-sub="credits"]`, `[data-sub="credits"] .about-head`. Delete the now
redundant `expect(await page.textContent('[data-win="motivation"] …'))` /
`infra` / `credits` title lines if the `subs` assertion above already covers
them. The last test in the file (`all panes stay reachable…`) is rewritten in
Step 3; for now change its two selectors to `[data-sub="hit"]` and
`[data-sub="credits"]`.

Also grep all of `pilgrim/tests/e2e/*.spec.ts` for `data-win="hit"`,
`"motivation"`, `"infra"`, `"credits"` and fix any other hit the same way.

### 1.4 Checks, commit

`make lint`, `make test`, `make e2e`. Look at the page once at 1280×720 (a
Playwright screenshot to the scratch dir is enough): ABOUT is one bordered
pane with four headed sub-sections, the diagram is as wide as the pane, the
footer still reads `© 2026 Pilgrim Dot Farm LLC`.

Commit: `feat(web): ABOUT pane holds hit counter, mission, infra, credits (UI_REFACTOR Step 1, RADIO §9.1)`.

---

## Step 2. Desktop left nav + active-section highlight (RADIO §9.1)

### 2.1 `index.html`

Directly after `</header>` and before `<main id="desktop">`:

```html
<nav id="nav-desktop" class="nav-desktop" aria-label="Sections">
  <button type="button" data-go="radio" aria-current="page">RADIO</button>
  <button type="button" data-go="requests">REQUEST LINE</button>
  <button type="button" data-go="sheep">SHEEP PEN</button>
  <button type="button" data-go="about">ABOUT</button>
</nav>
```

Add as the **last** script tag (after `chat.js`):

```html
<script src="shell.js?v=20261109a"></script>
```

Bump `titlebar.js?v=` and `style.css?v=`.

### 2.2 `titlebar.js`

In the first IIFE, inside `fit()`, after the `for` loop, add one line so CSS
can place the nav under a header whose height depends on wrapping:

```js
    document.documentElement.style.setProperty("--header-h", bar.offsetHeight + "px");
```

### 2.3 `style.css`

Add after the ABOUT block from Step 1:

```css
/* Desktop left nav (UI_REFACTOR.md Step 2, RADIO §9.1): fixed in the left
   margin so the 484 px column never moves. Right edge sits 24 px left of the
   column (column left = 50% - 242px); below 900 px the phone shell takes
   over (Step 3) and this is display:none. */
.nav-desktop { display: none; }
@media (min-width: 900px) {
  .nav-desktop {
    display: flex; flex-direction: column; gap: 6px;
    position: fixed; z-index: 6;
    top: calc(var(--header-h, 64px) + 16px);
    left: max(16px, calc(50% - 242px - 24px - 170px));
    width: 170px; padding: 10px;
    background: var(--panel); border: 1px solid var(--violet); border-radius: 8px;
    box-shadow: 0 0 24px rgba(166,61,255,.45), 0 0 6px var(--magenta);
  }
}
.nav-desktop button {
  font: inherit; font-weight: bold; font-size: 14px; letter-spacing: 1px;
  text-align: left; color: var(--dim); background: transparent;
  border: 1px solid transparent; border-radius: 6px; padding: 10px 12px;
  cursor: pointer;
}
.nav-desktop button:hover { color: var(--text); border-color: var(--violet); }
.nav-desktop button:focus-visible { outline: 2px solid var(--cyan); outline-offset: 1px; }
.nav-desktop button[aria-current="page"] {
  color: var(--cyan); border-color: var(--cyan); text-shadow: 0 0 8px var(--cyan);
  background: rgba(36,224,255,.08);
}
```

### 2.4 `shell.js` (new file, `pilgrim/web/shell.js`)

Write the whole file now; Steps 3 and 4 add to it at the marked places.

```js
/* Pilgrim Dot Farm — app shell (UI_REFACTOR.md, RADIO §9.1).
   Desktop (>= 900 px): a left nav that scrolls #desktop to a pane and
   highlights the pane in view. Phone (<= 899 px): one pane at a time, four
   bottom tabs, and a mini player that mirrors #btn-play / #indicator /
   pd:nowplaying. Nothing here touches audio: the mini PLAY button clicks the
   real #btn-play from inside its own click handler, so the AudioContext is
   still created in a user gesture (RADIO §9.2, AGENTS hard rules 1 and 9). */
(() => {
  "use strict";

  const MQ = matchMedia("(max-width: 899px)");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const desk = document.getElementById("desktop");
  const navD = document.getElementById("nav-desktop");
  if (!desk || !navD) return;
  const wins = Array.from(desk.querySelectorAll(":scope > .window"));
  const byName = {};
  wins.forEach(w => { byName[w.dataset.win] = w; });
  if (!wins.length) return;

  function mark(nav, name) {
    if (!nav) return;
    nav.querySelectorAll("[data-go]").forEach(b => {
      if (b.dataset.go === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  }

  /* ---------------- desktop: scroll + highlight ---------------- */
  function goTo(name) {
    const w = byName[name];
    if (!w) return;
    // #desktop is position:relative, so offsetTop is relative to it; 16 px is
    // its own top padding, so the pane lands flush with the first pane's y.
    desk.scrollTo({ top: Math.max(0, w.offsetTop - 16),
                    behavior: reduced.matches ? "auto" : "smooth" });
    mark(navD, name);
  }

  let raf = 0;
  function highlight() {
    raf = 0;
    if (MQ.matches) return;
    let name = wins[0].dataset.win;
    if (desk.scrollTop + desk.clientHeight >= desk.scrollHeight - 4) {
      name = wins[wins.length - 1].dataset.win;
    } else {
      const line = desk.scrollTop + desk.clientHeight * 0.35;
      for (const w of wins) if (w.offsetTop <= line) name = w.dataset.win;
    }
    mark(navD, name);
  }
  desk.addEventListener("scroll", () => {
    if (!raf) raf = requestAnimationFrame(highlight);
  }, { passive: true });
  addEventListener("resize", () => { if (!raf) raf = requestAnimationFrame(highlight); });

  /* ---------------- phone: one visible pane ---------------- */
  // STEP 3 INSERT A

  /* ---------------- mode + clicks ---------------- */
  function apply() {
    if (MQ.matches) {
      // STEP 3 INSERT B
    } else {
      wins.forEach(w => w.classList.remove("active"));
      highlight();
    }
  }
  if (MQ.addEventListener) MQ.addEventListener("change", apply);
  else MQ.addListener(apply);

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    const b = t && t.closest ? t.closest("[data-go]") : null;
    if (!b) return;
    if (MQ.matches) { /* STEP 3 INSERT C */ }
    else goTo(b.dataset.go);
  });

  // STEP 3 INSERT D (mini player)
  // STEP 4 INSERT E (visual viewport / keyboard)

  apply();
})();
```

### 2.5 Tests: new `pilgrim/tests/e2e/nav.spec.ts`

```ts
/** Desktop nav (UI_REFACTOR.md Step 2, RADIO §9.1): fixed in the left margin,
 *  never moves the column, scrolls to a pane, follows manual scrolling. */
import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });   // instant scrollTo
  await page.goto("/");
});

test("nav sits in the left margin and the column stays centred", async ({ page }) => {
  await expect(page.locator("#nav-desktop")).toBeVisible();
  await expect(page.locator("#footer")).toBeVisible();
  const m = await page.evaluate(() => {
    const desk = document.getElementById("desktop")!;
    const d = desk.getBoundingClientRect();
    const w = document.querySelector('[data-win="radio"]')!.getBoundingClientRect();
    const n = document.getElementById("nav-desktop")!.getBoundingClientRect();
    return { off: Math.abs((w.left + w.right) / 2 - (d.left + desk.clientWidth / 2)),
             width: w.width, navRight: n.right, colLeft: w.left };
  });
  expect(m.off).toBeLessThan(2);
  expect(m.width).toBe(484);
  expect(m.navRight).toBeLessThan(m.colLeft);
  const labels = await page.$$eval("#nav-desktop button", els => els.map(e => e.textContent));
  expect(labels).toEqual(["RADIO", "REQUEST LINE", "SHEEP PEN", "ABOUT"]);
});

test("clicking an entry scrolls to that pane and highlights it", async ({ page }) => {
  for (const name of ["about", "sheep", "requests", "radio"]) {
    await page.click(`#nav-desktop [data-go="${name}"]`);
    await expect(page.locator(`#nav-desktop [data-go="${name}"]`)).toHaveAttribute("aria-current", "page");
    await expect(page.locator("#nav-desktop [aria-current='page']")).toHaveCount(1);
    const top = await page.evaluate((n) => {
      const d = document.getElementById("desktop")!.getBoundingClientRect();
      const w = document.querySelector(`[data-win="${n}"]`)!.getBoundingClientRect();
      return w.top - d.top;
    }, name);
    // flush with the 16 px padding, or as close as the scroll range allows (about = end)
    if (name !== "about") expect(Math.abs(top - 16)).toBeLessThan(3);
    else expect(top).toBeLessThanOrEqual(16 + 1);
  }
});

test("highlight follows manual scrolling, both directions", async ({ page }) => {
  const desk = page.locator("#desktop");
  await desk.evaluate(el => el.scrollTo(0, el.scrollHeight));
  await expect(page.locator("#nav-desktop [data-go='about']")).toHaveAttribute("aria-current", "page");
  await desk.evaluate(el => {
    const w = document.querySelector('[data-win="sheep"]') as HTMLElement;
    el.scrollTo(0, w.offsetTop - 16);
  });
  await expect(page.locator("#nav-desktop [data-go='sheep']")).toHaveAttribute("aria-current", "page");
  await desk.evaluate(el => el.scrollTo(0, 0));
  await expect(page.locator("#nav-desktop [data-go='radio']")).toHaveAttribute("aria-current", "page");
});
```

### 2.6 Checks, commit

`make lint`, `make test`, `make e2e`. Confirm by eye (screenshot) that the
column has not moved from Step 1's screenshot.

Commit: `feat(web): desktop section nav with active highlight (UI_REFACTOR Step 2, RADIO §9.1)`.

---

## Step 3. Phone shell: header, tabs, one pane at a time, mini player (RADIO §9.1, §9.2)

### 3.1 `index.html`

Viewport meta:

```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
```

After `</footer>` (and before the inline year script):

```html
<div id="mini" class="mini idle">
  <button type="button" id="mini-title" class="mini-title" data-go="radio" aria-label="Open the radio">
    <span class="mini-dot" aria-hidden="true"></span>
    <span id="mini-text" class="mini-text">PILGRIM DOT FARM</span>
  </button>
  <button type="button" id="mini-play" class="mini-play" aria-label="Play">▶</button>
</div>
<nav id="nav-mobile" class="nav-mobile" aria-label="Sections">
  <button type="button" data-go="radio" aria-current="page">RADIO</button>
  <button type="button" data-go="requests">REQUEST</button>
  <button type="button" data-go="sheep">CHAT</button>
  <button type="button" data-go="about">ABOUT</button>
</nav>
```

Bump `shell.js?v=` and `style.css?v=`.

### 3.2 `style.css`: mini player and tab row (any mode; hidden on desktop)

Add after the `.nav-desktop` block:

```css
/* Phone chrome (UI_REFACTOR.md Step 3, RADIO §9.1): the mini player and tab
   row are flex children of body (never position:fixed, so they behave when
   iOS opens the keyboard). Hidden on desktop; the phone media block at the
   end of this file shows them. */
.mini, .nav-mobile { display: none; }
.mini {
  position: relative; z-index: 5; flex: none;
  align-items: center; gap: 8px; height: 48px; padding: 0 8px 0 12px;
  background: linear-gradient(90deg, rgba(255,47,214,.15), rgba(36,224,255,.08));
  border-top: 1px solid var(--violet);
}
.mini-title {
  flex: 1 1 auto; min-width: 0; height: 44px; padding: 0;
  display: flex; align-items: center; gap: 8px; text-align: left;
  font: inherit; font-size: 14px; letter-spacing: 1px; color: var(--text);
  background: transparent; border: 0; cursor: pointer;
}
.mini-text { flex: 1 1 auto; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.mini-dot { flex: none; width: 10px; height: 10px; border-radius: 50%; background: var(--dim); }
.mini.onair .mini-dot { background: var(--ok); animation: pulse 1s infinite; }
.mini.buffering .mini-dot { background: var(--cyan); animation: pulse-cyan .5s infinite; }
.mini.onair .mini-text { color: var(--ok); }
.mini-play {
  flex: none; width: 44px; height: 44px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font: inherit; font-size: 18px; cursor: pointer;
  color: #0a0118; background: var(--magenta); border: 1px solid var(--magenta);
  box-shadow: 0 0 12px rgba(255,47,214,.55);
}
.mini-play.stop { color: var(--magenta); background: rgba(10,1,24,.6);
  box-shadow: 0 0 8px rgba(255,47,214,.25); }
.mini-play:disabled { filter: grayscale(.7); cursor: not-allowed; }
.mini-play:focus-visible, .mini-title:focus-visible {
  outline: 2px solid var(--cyan); outline-offset: -2px; }

.nav-mobile {
  position: relative; z-index: 5; flex: none;
  padding: 0 0 env(safe-area-inset-bottom, 0);
  border-top: 1px solid var(--magenta); background: rgba(10,1,24,.96);
}
.nav-mobile button {
  flex: 1 1 0; min-width: 0; min-height: 52px; padding: 0 4px;
  font: inherit; font-weight: bold; font-size: 12px; letter-spacing: 1px;
  color: var(--dim); background: transparent; border: 0;
  border-top: 2px solid transparent; cursor: pointer;
}
.nav-mobile button[aria-current="page"] {
  color: var(--cyan); border-top-color: var(--cyan); text-shadow: 0 0 8px var(--cyan); }
.nav-mobile button:focus-visible { outline: 2px solid var(--cyan); outline-offset: -2px; }
```

### 3.3 `style.css`: the phone media block

Append at the **very end** of `style.css` (it must come after the existing
`@media (max-width: 520px)` and `@media (max-width: 420px)` blocks so its
equal-specificity rules win):

```css
/* ============================================================
   Phone shell (UI_REFACTOR.md Step 3, RADIO §9.1). One breakpoint, 899 px:
   header (one line, no tagline) / the active pane / mini player / tabs. The
   footer is hidden; ABOUT ends with its copyright line. All four panes stay
   laid out (visibility, not display), so scroll positions, the chat log's
   follow-the-bottom logic and typed text survive tab changes. --vvh is the
   visual viewport height, kept by shell.js (Step 4); 100dvh is the fallback.
   ============================================================ */
@media (max-width: 899px) {
  html, body { height: var(--vvh, 100dvh); }
  body { overscroll-behavior: none; }

  #titlebar { padding: 8px 12px; column-gap: 10px; align-items: center; --logo-fs: clamp(12px, 4.2vw, 19px); }
  #titlebar .tagline { display: none; }
  #titlebar .logo { white-space: nowrap; overflow: hidden; letter-spacing: 1px; }

  #footer { display: none; }
  .about-copyright {
    display: block; padding: 14px; text-align: center;
    color: var(--cyan); font-size: 12px; font-weight: bold; letter-spacing: 1px;
    border-top: 1px solid var(--violet);
  }

  #desktop { display: block; position: relative; padding: 0; overflow: hidden; }
  #desktop > .window {
    position: absolute; inset: 0; width: auto; visibility: hidden;
    display: flex; flex-direction: column;
    border: 0; border-radius: 0; box-shadow: none;
  }
  #desktop > .window.active { visibility: visible; }
  /* No title bars on phones (D14): the tabs name the pane. SHEEP PEN keeps a
     thin status strip because chat.js writes the connection state into
     #chat-status, which lives in that bar. */
  .win-bar { display: none; }
  [data-win="sheep"] .win-bar { display: flex; flex: none; justify-content: flex-end;
    padding: 3px 12px; background: transparent; border-bottom: 1px solid rgba(166, 61, 255, .35); }
  [data-win="sheep"] .win-bar .win-title { display: none; }
  #desktop > .window > .win-body {
    flex: 1 1 auto; min-height: 0; overflow-y: auto;
    overscroll-behavior: contain; -webkit-overflow-scrolling: touch;
  }

  /* RADIO fits the screen: only the art shrinks (D8, budget in §0.5). The
     rate row, play row and meter keep --art-w. Short viewports scroll. */
  .win-body.radio { padding: 10px 14px; gap: 8px; justify-content: flex-start; }
  .win-body.radio .art-frame {
    width: min(var(--art-w, min(100%, 300px)), max(140px, calc(var(--vvh, 100dvh) - 440px)));
  }

  /* CHAT fills the pane: the log takes the remaining height, composer pinned. */
  .win-body.sheep { gap: 8px; }
  .win-body.sheep .chat-log { flex: 1 1 auto; min-height: 120px; max-height: none; }

  .mini, .nav-mobile { display: flex; }
  body.kbd-open .mini, body.kbd-open .nav-mobile { display: none; }
}
```

### 3.4 `shell.js` inserts

Replace `// STEP 3 INSERT A` with:

```js
  const navM = document.getElementById("nav-mobile");
  const TAB_KEY = "pd_tab";
  let active = "radio";
  try {
    const s = sessionStorage.getItem(TAB_KEY);
    if (s && byName[s]) active = s;
  } catch (e) { /* private mode */ }

  function show(name) {
    if (!byName[name]) return;
    active = name;
    wins.forEach(w => w.classList.toggle("active", w.dataset.win === name));
    mark(navM, name);
    try { sessionStorage.setItem(TAB_KEY, name); } catch (e) { /* ignore */ }
  }
```

Replace `// STEP 3 INSERT B` with:

```js
      show(active);
```

Replace `/* STEP 3 INSERT C */` with:

```js
show(b.dataset.go);
```

Replace `// STEP 3 INSERT D (mini player)` with:

```js
  /* ---------------- mini player: a mirror of the radio pane ---------------- */
  const mini = document.getElementById("mini");
  const miniPlay = document.getElementById("mini-play");
  const miniText = document.getElementById("mini-text");
  const btn = document.getElementById("btn-play");
  const ind = document.getElementById("indicator");
  let song = null;   // the audible named song from pd:nowplaying, or null

  function syncMini() {
    if (!mini || !miniPlay || !miniText || !btn || !ind) return;
    const playing = btn.classList.contains("stop");
    miniPlay.textContent = playing ? "■" : "▶";
    miniPlay.classList.toggle("stop", playing);
    miniPlay.setAttribute("aria-label", playing ? "Stop" : "Play");
    miniPlay.disabled = btn.disabled;
    const st = ind.classList.contains("onair") ? "onair"
             : ind.classList.contains("buffering") ? "buffering" : "idle";
    mini.classList.remove("onair", "buffering", "idle");
    mini.classList.add(st);
    if (song) miniText.textContent = song.title + " — " + song.artist;
    else if (st === "buffering") miniText.textContent = "BUFFERING…";
    else if (st === "onair") miniText.textContent = "ON AIR";
    else miniText.textContent = btn.disabled ? "OFF THE AIR" : "PILGRIM DOT FARM";
  }
  document.addEventListener("pd:nowplaying", (ev) => {
    const d = ev.detail;
    song = (d && d.title && d.artist) ? d : null;
    syncMini();
  });
  if (btn && ind && window.MutationObserver) {
    new MutationObserver(syncMini).observe(btn, { attributes: true, attributeFilter: ["class", "disabled"] });
    new MutationObserver(syncMini).observe(ind, { attributes: true, attributeFilter: ["class"] });
  }
  if (miniPlay && btn) miniPlay.addEventListener("click", () => btn.click());
  syncMini();
```

Note `#mini-title` carries `data-go="radio"`, so the document click handler
already switches to RADIO; do not add a second handler for it.

### 3.5 Existing tests that fall into phone mode

These set viewports ≤ 899 px, so panes other than RADIO are now hidden until
their tab is tapped. Make exactly these edits.

`cosmetic.spec.ts`, last test, replace whole test:

```ts
test("phone: panes are tabs; ABOUT scrolls and ends with the copyright", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await page.goto("/");
  await expect(page.locator('[data-win="radio"]')).toBeVisible();
  await expect(page.locator('[data-win="about"]')).toBeHidden();
  await page.click('#nav-mobile [data-go="about"]');
  await expect(page.locator('[data-win="about"]')).toBeVisible();
  await expect(page.locator('[data-win="radio"]')).toBeHidden();
  const body = page.locator('[data-win="about"] > .win-body');
  await body.evaluate(el => el.scrollTo(0, el.scrollHeight));
  await expect(page.locator('[data-sub="credits"]')).toBeVisible();
  await expect(page.locator(".about-copyright")).toBeVisible();
  await expect(page.locator(".about-copyright")).toContainText("Pilgrim Dot Farm LLC");
  await expect(page.locator("#footer")).toBeHidden();
});
```

`chat.spec.ts`, test `mobile layout keeps the pen usable at 320 px`: after
`await page.goto("/");` add

```ts
  await page.click('#nav-mobile [data-go="sheep"]');
```

`paid.spec.ts`, `openPanel`:

```ts
async function openPanel(page: Page) {
  await page.goto("/");
  const tab = page.locator('#nav-mobile [data-go="requests"]');
  if (await tab.isVisible()) await tab.click();
  await expect(page.locator("#pay-panel")).toBeVisible();
}
```

`voting.spec.ts`, test `mobile: no horizontal scroll, 40px vote targets,
status under the title`: after `await page.goto("/");` inside the loop add
`await page.click('#nav-mobile [data-go="requests"]');`; before
`const rate = page.locator("#rate-row");` add
`await page.click('#nav-mobile [data-go="radio"]');`; and replace the art
comparison

```ts
    const art = (await page.locator(".art-frame").boundingBox())!.width;
    expect(rw).toBeLessThanOrEqual(art + 1);
```

with

```ts
    // the rating row never overflows the pane's content box (on phones the
    // art may be narrower than the row: UI_REFACTOR.md D8)
    const inner = await page.locator(".win-body.radio").evaluate(el =>
      el.clientWidth - parseFloat(getComputedStyle(el).paddingLeft) - parseFloat(getComputedStyle(el).paddingRight));
    expect(rw).toBeLessThanOrEqual(inner + 1);
```

Run `make e2e`. Any other test that fails only because a pane is hidden
gets the same one-line tab click, nothing more; report each such edit.

### 3.6 New phone project + `mobile.spec.ts`

`playwright.config.ts`, replace the `projects:` line:

```ts
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] }, testIgnore: /mobile\.spec\.ts/ },
    // UI_REFACTOR.md D1: one standard phone, Chromium with iPhone 14 metrics
    // (390x664, 3x, mobile UA, touch). Real Safari/Android = operator check.
    { name: "phone", use: { ...devices["iPhone 14"], browserName: "chromium", channel },
      testMatch: /mobile\.spec\.ts/ },
  ],
```

New `pilgrim/tests/e2e/mobile.spec.ts`:

```ts
/** Phone shell (UI_REFACTOR.md Step 3, RADIO §9.1/§9.2) on one standard phone
 *  (iPhone 14 metrics in Chromium). Fakes only (e2e_server). */
import { test, expect, type Page } from "@playwright/test";

const tab = (page: Page, name: string) => page.locator(`#nav-mobile [data-go="${name}"]`).tap();

test.beforeEach(async ({ page }) => {
  // Keep every AudioContext ever constructed: navigation must never make a
  // second one, and the first must stay RUNNING with its clock advancing (a
  // context that merely suspends would pass a construction count).
  await page.addInitScript(() => {
    const Orig = window.AudioContext;
    const ctxs: AudioContext[] = [];
    (window as any).__ctxs = ctxs;
    (window as any).__ctxCount = () => ctxs.length;
    (window as any).__ctxProbe = () => ctxs.map(c => ({ state: c.state, t: c.currentTime }));
    window.AudioContext = class extends Orig {
      constructor(...a: any[]) { super(...a); ctxs.push(this); }
    } as any;
  });
  await page.goto("/");
});

test("shell: one-line header, no footer, tabs + mini player, RADIO fits", async ({ page }) => {
  await expect(page.locator("#titlebar .tagline")).toBeHidden();
  await expect(page.locator("#titlebar .logo")).toBeVisible();
  const logo = await page.locator("#titlebar .logo").evaluate(el => ({ sw: el.scrollWidth, cw: el.clientWidth, h: el.getBoundingClientRect().height }));
  expect(logo.sw).toBeLessThanOrEqual(logo.cw + 1);      // one line, not clipped
  expect(logo.h).toBeLessThan(30);
  await expect(page.locator("#footer")).toBeHidden();
  await expect(page.locator("#nav-desktop")).toBeHidden();
  await expect(page.locator("#nav-mobile")).toBeVisible();
  await expect(page.locator("#mini")).toBeVisible();
  // no pane title bars on phones (D14); the tabs name the pane
  await expect(page.locator('[data-win="radio"] .win-bar')).toBeHidden();
  const labels = await page.$$eval("#nav-mobile button", els => els.map(e => e.textContent));
  expect(labels).toEqual(["RADIO", "REQUEST", "CHAT", "ABOUT"]);
  for (const b of await page.locator("#nav-mobile button, #mini-play").all()) {
    const box = (await b.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.width).toBeGreaterThanOrEqual(44);
  }
  await expect(page.locator('[data-win="radio"]')).toHaveClass(/active/);
  await expect(page.locator('[data-win="requests"]')).toBeHidden();
  const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(over).toBe(false);
  // the whole shell fits the viewport and the radio body needs no scrolling (D8)
  const fit = await page.evaluate(() => {
    const nav = document.getElementById("nav-mobile")!.getBoundingClientRect();
    const body = document.querySelector('[data-win="radio"] > .win-body')!;
    return { navBottom: nav.bottom, sh: body.scrollHeight, ch: body.clientHeight };
  });
  expect(fit.navBottom).toBeLessThanOrEqual(664 + 1);
  expect(fit.sh).toBeLessThanOrEqual(fit.ch + 1);
  const art = (await page.locator(".art-frame").boundingBox())!;
  expect(art.width).toBeGreaterThanOrEqual(140);
  expect(Math.abs(art.width - art.height)).toBeLessThan(2);
});

test("tabs switch panes; the selected tab is marked; state survives", async ({ page }) => {
  for (const [name, title] of [["requests", "REQUEST LINE"], ["sheep", "SHEEP PEN"], ["about", "ABOUT"], ["radio", "RADIO BROADCAST"]]) {
    await tab(page, name);
    await expect(page.locator(`[data-win="${name}"]`)).toBeVisible();
    await expect(page.locator(`[data-win="${name}"] .win-title`)).toHaveText(title);   // present for desktop, hidden here
    await expect(page.locator(`[data-win="${name}"] .win-title`)).toBeHidden();
    await expect(page.locator(`#nav-mobile [data-go="${name}"]`)).toHaveAttribute("aria-current", "page");
    await expect(page.locator("#nav-mobile [aria-current='page']")).toHaveCount(1);
    await expect(page.locator("#desktop > .window.active")).toHaveCount(1);
  }
  // typed text and a scroll position survive leaving and returning
  await tab(page, "requests");
  await page.fill("#req-input", "keep me");
  await tab(page, "about");
  const about = page.locator('[data-win="about"] > .win-body');
  await about.evaluate(el => el.scrollTo(0, 300));
  await tab(page, "sheep");
  await tab(page, "requests");
  await expect(page.locator("#req-input")).toHaveValue("keep me");
  await tab(page, "about");
  expect(await about.evaluate(el => el.scrollTop)).toBe(300);
  await expect(page.locator(".about-copyright")).toBeAttached();
  // the tab is remembered across a reload (D11)
  await page.reload();
  await expect(page.locator('[data-win="about"]')).toHaveClass(/active/);
});

test("CHAT fills the pane: log scrolls inside it, composer at the bottom", async ({ page }) => {
  await tab(page, "sheep");
  const m = await page.evaluate(() => {
    const pane = document.querySelector('[data-win="sheep"]')!.getBoundingClientRect();
    const body = document.querySelector('[data-win="sheep"] > .win-body')!;
    const log = document.getElementById("chat-log")!.getBoundingClientRect();
    const compose = document.querySelector(".chat-compose")!.getBoundingClientRect();
    const mini = document.getElementById("mini")!.getBoundingClientRect();
    return { paneBottom: pane.bottom, bodyScrolls: body.scrollHeight > body.clientHeight + 1,
             logH: log.height, composeBottom: compose.bottom, miniTop: mini.top };
  });
  expect(m.bodyScrolls).toBe(false);
  expect(m.logH).toBeGreaterThan(120);
  expect(m.composeBottom).toBeLessThanOrEqual(m.miniTop + 1);
  // the title bar is gone but the connection status strip stays (D14)
  await expect(page.locator('[data-win="sheep"] .win-title')).toBeHidden();
  await expect(page.locator("#chat-status")).toBeVisible();
  await expect(page.locator("#chat-status")).not.toHaveText("");
});

test("mini player mirrors the radio pane and never restarts audio", async ({ page }) => {
  await expect(page.locator("#mini-text")).toHaveText("PILGRIM DOT FARM");
  await expect(page.locator("#mini-play")).toHaveAttribute("aria-label", "Play");
  await page.locator("#mini-play").tap();
  await expect(page.locator("#btn-play")).toHaveClass(/stop/);
  await expect(page.locator("#mini-play")).toHaveAttribute("aria-label", "Stop");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  await expect(page.locator("#mini")).toHaveClass(/onair/);
  await expect(page.locator("#song-label")).not.toHaveText("", { timeout: 20_000 });
  const title = (await page.textContent("#song-title"))!;
  await expect(page.locator("#mini-text")).toContainText(title);
  const probe = () => page.evaluate(() => (window as any).__ctxProbe() as { state: string; t: number }[]);
  const before = await probe();
  expect(before).toHaveLength(1);
  expect(before[0].state).toBe("running");

  // tour every tab while on air: still on air, the SAME context, still
  // running, its clock moving, and the single #sink untouched
  for (const name of ["sheep", "requests", "about", "radio"]) {
    await tab(page, name);
    await page.waitForTimeout(700);
    await expect(page.locator("#indicator")).toHaveClass(/onair/);
    await expect(page.locator("#mini")).toBeVisible();
    const now = await probe();
    expect(now, `a second AudioContext appeared on the ${name} tab`).toHaveLength(1);
    expect(now[0].state, `AudioContext not running on the ${name} tab`).toBe("running");
  }
  const after = await probe();
  expect(after).toHaveLength(1);
  expect(after[0].state).toBe("running");
  // four tabs x 700 ms of waiting: the audio clock must have moved at least 2 s
  expect(after[0].t - before[0].t).toBeGreaterThan(2);
  expect(await page.evaluate(() => document.querySelectorAll("audio").length)).toBe(1);
  expect(await page.evaluate(() => document.getElementById("sink")!.getAttribute("id"))).toBe("sink");

  // the title area navigates, the play button does not
  await tab(page, "about");
  await page.locator("#mini-title").tap();
  await expect(page.locator('[data-win="radio"]')).toHaveClass(/active/);
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  await tab(page, "sheep");
  await page.locator("#mini-play").tap();
  await expect(page.locator('[data-win="sheep"]')).toHaveClass(/active/);
  await expect(page.locator("#indicator")).toHaveClass(/idle/);
  await expect(page.locator("#mini-play")).toHaveAttribute("aria-label", "Play");
  await expect(page.locator("#mini-text")).toHaveText("PILGRIM DOT FARM");
  // STOP closes the one context (player.js stop(): ctx.close()); still only one
  const closed = await probe();
  expect(closed).toHaveLength(1);
  expect(closed[0].state).toBe("closed");
});

test("OFF AIR disables the mini PLAY too", async ({ page }) => {
  await page.route("**/api/health", r => r.fulfill({ status: 503, contentType: "application/json",
    body: JSON.stringify({ on_air: false, off_air: true }) }));
  await page.reload();
  await expect(page.locator("#btn-play")).toBeDisabled({ timeout: 10_000 });
  await expect(page.locator("#mini-play")).toBeDisabled();
  await expect(page.locator("#mini-text")).toHaveText("OFF THE AIR");
});
```

If `offair.spec.ts` already shows a different way to force OFF AIR against
the e2e server, use that instead of `page.route` in the last test (adapt).

### 3.7 Checks, commit

`make lint`, `make test`, `make e2e` (both projects run; report counts per
project). Screenshots at 390×664 of each tab to the scratch dir and look at
them: one-line header, pane fills, mini player + tabs at the bottom, no
footer, nothing clipped on RADIO.

If the "RADIO fits" assertion (`sh <= ch + 1`) fails by a few pixels, the
budget in §0.5 was off for this font: raise the `440px` in the `.art-frame`
rule (3.3) in 10 px steps until it passes. Never loosen the test, and never
shrink anything but the art.

Commit: `feat(web): phone app shell: tabs, one pane at a time, mini player (UI_REFACTOR Step 3, RADIO §9.1, §9.2)`.

---

## Step 4. Keyboard and visual viewport on the phone (RADIO §9.1)

### 4.1 `shell.js`: replace `// STEP 4 INSERT E (visual viewport / keyboard)`

```js
  /* ---------------- visual viewport / keyboard (D6) ----------------
     The shell is as tall as the VISUAL viewport (iOS keeps the layout
     viewport under the keyboard; Android resizes it with
     interactive-widget=resizes-content). While a text field has focus and the
     keyboard has taken >= 120 px, body.kbd-open hides the tabs + mini player
     so the composer sits right above the keys. iOS also scrolls the window to
     reveal the field; we pin it back so the shell never drifts off-screen. */
  const vv = window.visualViewport;
  let baseH = 0, baseW = 0;
  function typing() {
    const el = document.activeElement;
    return !!el && /^(INPUT|TEXTAREA)$/.test(el.tagName);
  }
  function viewport() {
    const root = document.documentElement;
    if (!MQ.matches) {
      root.style.removeProperty("--vvh");
      document.body.classList.remove("kbd-open");
      return;
    }
    const h = vv ? vv.height : innerHeight;
    if (innerWidth !== baseW) { baseW = innerWidth; baseH = 0; }   // rotation
    if (!typing()) baseH = Math.max(baseH, h);
    root.style.setProperty("--vvh", Math.round(h) + "px");
    document.body.classList.toggle("kbd-open", typing() && baseH - h >= 120);
    if ((vv && vv.offsetTop > 0) || window.scrollY > 0) window.scrollTo(0, 0);
  }
  if (vv) {
    vv.addEventListener("resize", viewport);
    vv.addEventListener("scroll", viewport);
  }
  addEventListener("resize", viewport);
  addEventListener("orientationchange", () => setTimeout(viewport, 300));
  document.addEventListener("focusin", (ev) => {
    const el = ev.target;
    if (!MQ.matches || !el || !/^(INPUT|TEXTAREA)$/.test(el.tagName)) return;
    // after the keyboard animation: resize the shell, then bring the field
    // inside its scrolling pane body (REQUEST textareas, the name row)
    setTimeout(() => {
      viewport();
      try { el.scrollIntoView({ block: "nearest" }); } catch (e) { /* old browsers */ }
    }, 350);
  });
  document.addEventListener("focusout", () => setTimeout(viewport, 100));
  if (MQ.addEventListener) MQ.addEventListener("change", viewport);
  else MQ.addListener(viewport);
  viewport();
```

### 4.2 Tests: append to `mobile.spec.ts`

Playwright cannot raise a real keyboard; these pin the CSS contract (the
shell follows `--vvh`, `kbd-open` frees the bottom) and the focus scroll.

```ts
test("keyboard contract: shell follows --vvh and frees the bottom while typing", async ({ page }) => {
  await tab(page, "sheep");
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--vvh", "400px");
    document.body.classList.add("kbd-open");
  });
  await expect(page.locator("#nav-mobile")).toBeHidden();
  await expect(page.locator("#mini")).toBeHidden();
  const compose = (await page.locator(".chat-compose").boundingBox())!;
  expect(compose.y + compose.height).toBeLessThanOrEqual(400 + 1);
  await expect(page.locator("#chat-name-input")).toBeVisible();
  await page.evaluate(() => {
    document.documentElement.style.removeProperty("--vvh");
    document.body.classList.remove("kbd-open");
  });
  await expect(page.locator("#nav-mobile")).toBeVisible();
});

test("focusing a REQUEST field scrolls it into the pane", async ({ page }) => {
  await tab(page, "requests");
  await page.locator("#pay-art").focus();
  await page.waitForTimeout(500);
  const m = await page.evaluate(() => {
    const body = document.querySelector('[data-win="requests"] > .win-body')!.getBoundingClientRect();
    const f = document.getElementById("pay-art")!.getBoundingClientRect();
    return { top: f.top - body.top, bottom: body.bottom - f.bottom };
  });
  expect(m.top).toBeGreaterThanOrEqual(-1);
  expect(m.bottom).toBeGreaterThanOrEqual(-1);
});
```

### 4.3 Checks, commit

`make lint`, `make test`, `make e2e`.

Commit: `feat(web): phone shell follows the visual viewport; keyboard frees the bottom (UI_REFACTOR Step 4, RADIO §9.1)`.

---

## Step 5. Desktop regression pass and the breakpoint (RADIO §9.1)

No new feature. Prove nothing moved on desktop and that the mode switch is
clean.

1. Append to `nav.spec.ts`:

```ts
test("crossing the breakpoint while on air never touches the AudioContext", async ({ page }) => {
  await page.addInitScript(() => {
    const Orig = window.AudioContext;
    const ctxs: AudioContext[] = [];
    (window as any).__ctxProbe = () => ctxs.map(c => ({ state: c.state, t: c.currentTime }));
    window.AudioContext = class extends Orig { constructor(...a: any[]) { super(...a); ctxs.push(this); } } as any;
  });
  const probe = () => page.evaluate(() => (window as any).__ctxProbe() as { state: string; t: number }[]);
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  const before = await probe();
  expect(before).toHaveLength(1);
  expect(before[0].state).toBe("running");
  await page.setViewportSize({ width: 820, height: 720 });     // phone shell
  await expect(page.locator("#nav-mobile")).toBeVisible();
  await expect(page.locator('[data-win="radio"]')).toHaveClass(/active/);
  await page.click('#nav-mobile [data-go="sheep"]');
  await page.setViewportSize({ width: 1280, height: 720 });    // desktop again
  await expect(page.locator("#nav-desktop")).toBeVisible();
  await expect(page.locator("#nav-mobile")).toBeHidden();
  await expect(page.locator('[data-win="sheep"]')).toBeVisible();
  await expect(page.locator('[data-win="radio"]')).toBeVisible();
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  await page.waitForTimeout(1500);
  const after = await probe();
  expect(after, "a second AudioContext appeared across the breakpoint").toHaveLength(1);
  expect(after[0].state).toBe("running");
  expect(after[0].t - before[0].t, "the audio clock stalled across the breakpoint").toBeGreaterThan(1);
});

test("at 900 px the nav still clears the column; at 899 px the phone shell shows", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 720 });
  await page.goto("/");
  const m = await page.evaluate(() => ({
    navRight: document.getElementById("nav-desktop")!.getBoundingClientRect().right,
    colLeft: document.querySelector('[data-win="radio"]')!.getBoundingClientRect().left,
  }));
  expect(m.navRight).toBeLessThan(m.colLeft);
  await page.setViewportSize({ width: 899, height: 720 });
  await expect(page.locator("#nav-desktop")).toBeHidden();
  await expect(page.locator("#nav-mobile")).toBeVisible();
});
```

2. Run the full `make e2e` and compare desktop screenshots at 1280×720 and
   1366×768 against Step 1's: the column, header and footer are unchanged.

3. Checks, commit: `test(web): breakpoint + desktop regression for the app shell (UI_REFACTOR Step 5, RADIO §9.1)`.

---

## Step 6. Docs, pointers, deploy handoff

1. **`RADIO.md` §9.1.** Replace the opening sentence *"Retro window shell on
   a vertical stack, scrollable `#desktop` so every pane stays reachable on
   small screens:"* with:

   > Retro window shell. On desktop (≥ 900 px) the panes are a vertical
   > stack in the scrollable `#desktop` with a fixed left **section nav**
   > (RADIO / REQUEST LINE / SHEEP PEN / ABOUT) in the margin that scrolls
   > to a pane and highlights the one in view; the 484 px column, header
   > and footer are unchanged by the nav. On phones (≤ 899 px) the page is
   > an app (UI_REFACTOR.md): a one-line header (hat + station name, no
   > tagline), **one pane at a time** filling the screen, a **mini player**
   > (state dot, audible song as *Title — Artist*, round PLAY/STOP) and four
   > bottom tabs **RADIO / REQUEST / CHAT / ABOUT**; the footer is hidden
   > and its copyright line ends the ABOUT pane. Inactive panes stay laid
   > out (visibility, not display) so scroll positions, chat follow-the-
   > bottom and typed text survive a tab change; the selected tab persists
   > in `sessionStorage` `pd_tab`. The shell is sized by the visual viewport
   > (`--vvh`); while a text field has focus and the keyboard is up the
   > tabs and mini player hide so the composer sits above the keys. The
   > mini player is a mirror of `#btn-play`, `#indicator` and
   > `pd:nowplaying` and its PLAY clicks the real button inside the gesture;
   > `player.js`, `#sink` and the AudioContext are untouched (§9.2). On
   > phones only the cover art shrinks (floor 140 px) so RADIO fits without
   > scrolling; the rate row, play row and meter keep their width, so the
   > indicator/art edge alignment below is a desktop rule. Pane title bars
   > are hidden on phones (the tabs name the pane); SHEEP PEN's bar survives
   > as a thin status strip carrying only the connection state. Landscape
   > phones stay in the phone shell. The panes:

   Add a bullet **ABOUT** after THE SHEEP PEN:

   > - **ABOUT** (UI_REFACTOR.md, 2026-11): one pane holding, in this order
   >   and with these exact sub-headings, **HIT COUNTER & SOCIAL**,
   >   **CORPORATE MISSION STATEMENT**, **INFRA AND DATA FLOW**, **CREDITS**
   >   (the former standalone panes, content unchanged), and on phones a
   >   final `© <year> Pilgrim Dot Farm LLC` line.

   Prefix the existing **MODEL CREDITS** and **HIT COUNTER** bullets with
   *"(sub-section of ABOUT)"*. Do not change anything else in §9.1 or §9.2.

2. **`AGENTS.md`** "Active work plans": add `UI_REFACTOR.md (Steps 1–6, app
   shell: ABOUT pane, desktop nav, phone tabs + mini player)` to the first
   paragraph.

3. **Checks, all of AGENTS §7:** `make lint`, `make test`, `make sim`,
   `make e2e`. Paste the counts in the report.

4. **Deploy (the agent runs it; D13/D15/D16):**
   - Web tier (Fargate): `make image-smoke`, `make image`, push
     `pilgrimdotfarm/front-end:<sha>` and `:latest` (Docker Hub; no ECR),
     bump `ecs_release` 14→15 in
     `~/projects/terraform-aws-pilgrim/modules/ec2/variables.tf`, apply,
     then vet the running container with the AWS CLI (task definition
     revision, `release` label, PRIMARY deployment `COMPLETED`, running
     count, `/api/health`). Record the digest line in the **Shipped**
     section at the end of this file, as SHEEP_PEN_PLAN.md does.
   - EC2 is gone and no nginx serves the front-end (D17): there is no edge
     checkout to `git pull`. The Fargate web tier is the only web surface.
   - No station restart is needed: nothing under `pilgrim/*.py` changed.
   - **Real-phone check (D1, D6, D16), operator, after the deploy:** iPhone
     Safari and one Android Chrome:
     header is one line; RADIO fits; PLAY from the mini player starts audio;
     lock the phone, audio continues (RADIO §9.2, unchanged behaviour); open
     the CHAT composer: keyboard up, composer visible above it, tabs hidden,
     tabs back on blur; same for a REQUEST textarea; rotate once.

5. Commit: `docs: app shell in RADIO §9.1; UI_REFACTOR.md pointers (UI_REFACTOR Step 6)`.

---

## 7. Acceptance checklist (the operator ticks these)

- [ ] Desktop 1280×720 / 1366×768: column, header, footer pixel-identical to
      before except the ABOUT pane; nav in the margin; click + scroll
      highlight work; nothing overlaps at 900 px.
- [ ] Phone 390×664: one-line header, no tagline, no footer, no pane
      title bars (chat status strip still visible); tabs + mini
      player; RADIO fits without scrolling; CHAT fills with the composer
      pinned; REQUEST and ABOUT scroll inside the pane; ABOUT ends with the
      copyright; no horizontal scroll.
- [ ] Audio: one `AudioContext`, still `running` with `currentTime`
      advancing, one `#sink`, on air across every tab change and across
      the breakpoint; mini PLAY/STOP mirrors and drives
      the real button; title tap opens RADIO without touching playback;
      OFF AIR disables both PLAY buttons.
- [ ] State: typed text, chat identity/history/scroll, queue scroll cue all
      survive tab changes; the tab survives a reload.
- [ ] `player.js`, `chat.js`, `rating.js`, `requests.js`, `pay.js`,
      `votes.js`, `voter.js`, `visitors.js`, every `.py`, Dockerfile,
      Terraform: untouched (`git diff --stat` shows only `index.html`,
      `style.css`, `titlebar.js`, `shell.js`, e2e files, `RADIO.md`,
      `AGENTS.md`, this file).
- [ ] `make lint`, `make test`, `make sim`, `make e2e` (both projects) pass.
- [ ] The agent has deployed (D13/D15) and recorded the digest in **Shipped**;
      the operator has done the real-phone check (D16).

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

Filled in by the agent at deploy time (D15; SHEEP_PEN_PLAN.md convention).

- Date: 2026-11-08
- Commit / image tag: `526d3c9` → `pilgrimdotfarm/front-end:526d3c9` and `:latest`
- Digest: `sha256:42ca837f75ece2644d16c4dc0fcb1bcb7f101fbf142c5d443c7f73ace652bc5f`
- `ecs_release`: 14 → 15 (`pg-app:11`, ARM64, release label 15)
- Task definition: `pg-app:11`; service `pg-app` PRIMARY deployment `COMPLETED`,
  1/1 running, task `RUNNING`/`HEALTHY`; `https://pilgrim.farm/` serves
  `shell.js?v=20261109a` and the ABOUT pane
- Operator real-phone check (D16): pending (iPhone Safari + one Android Chrome;
  see Step 6.4)

**Feedback round (2026-11-08, UI_REFACTOR_FEEDBACK.md):** commit `4073bee`,
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
