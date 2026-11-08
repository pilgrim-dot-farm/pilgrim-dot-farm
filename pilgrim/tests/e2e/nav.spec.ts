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
