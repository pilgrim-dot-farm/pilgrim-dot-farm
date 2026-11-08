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

test("a fresh page always reads RADIO, even on a very tall viewport", async ({ page }) => {
  // the bug: at scrollTop 0 the 35% line can land past REQUEST LINE on a tall
  // window (feedback 2026-11-08)
  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto("/");
  await expect(page.locator("#nav-desktop [data-go='radio']")).toHaveAttribute("aria-current", "page");
});

test("highlight follows manual scrolling, both directions", async ({ page }) => {
  await expect(page.locator("#nav-desktop [data-go='radio']")).toHaveAttribute("aria-current", "page");
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
