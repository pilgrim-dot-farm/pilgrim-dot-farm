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
