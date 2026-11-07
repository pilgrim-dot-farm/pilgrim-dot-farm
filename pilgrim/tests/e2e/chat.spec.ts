import { expect, test, type Browser, type Page } from "@playwright/test";

/* SHEEP_PEN.md §12: the Sheep Pen through the real client and the fakes e2e
   server. Every identity-sensitive scenario gets a fresh browser context; two
   participants are two contexts (ordinary tabs share localStorage). */

async function claim(page: Page, name: string) {
  await page.goto("/");
  await page.fill("#chat-name-input", name);
  await page.click("#chat-name-btn");
  await expect(page.locator("#chat-status")).toHaveText("GRAZE UPON IT", { timeout: 10000 });
  await expect(page.locator("#chat-input")).toBeEnabled();
  // a claimed name is locked: no client-side rename path
  await expect(page.locator("#chat-name-input")).toBeDisabled();
  await expect(page.locator("#chat-name-btn")).toHaveText("Claimed");
  await expect(page.locator("#chat-name-btn")).toBeDisabled();
}

async function participant(browser: Browser, name: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await claim(page, name);
  return { context, page };
}

test("read-only before naming: input disabled, log empty", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#chat-input")).toBeDisabled();
  await expect(page.locator("#chat-send")).toBeDisabled();
  await expect(page.locator("#chat-log .chat-msg")).toHaveCount(0);
});

test("mobile layout keeps the pen usable at 320 px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto("/");
  await expect(page.locator("[data-win='sheep']")).toBeVisible();
  await expect(page.locator("#chat-name-input")).toBeVisible();
  await expect(page.locator("#chat-input")).toBeVisible();
  await expect(page.locator("#chat-count")).toHaveText("0/200");
});

test("two participants send and receive", async ({ browser }) => {
  const a = await participant(browser, "AdaMobile");
  const b = await participant(browser, "BramMobile");
  try {
    await a.page.fill("#chat-input", "hello from ada");
    await a.page.click("#chat-send");
    await expect(b.page.locator("#chat-log", { hasText: "hello from ada" })).toBeVisible({ timeout: 10000 });
    await expect(a.page.locator("#chat-log", { hasText: "hello from ada" })).toBeVisible();

    await b.page.fill("#chat-input", "hello from bram");
    await b.page.click("#chat-send");
    await expect(a.page.locator("#chat-log", { hasText: "hello from bram" })).toBeVisible({ timeout: 10000 });

    // each display name gets its own palette colour, not one shared cyan
    const colors = await a.page.$$eval(
      ".chat-msg:not(.giles) .chat-author",
      (els) => els.map((e) => getComputedStyle(e).color));
    expect(colors.length).toBeGreaterThanOrEqual(2);
    expect(new Set(colors).size).toBeGreaterThanOrEqual(2);
    expect(colors).not.toContain("rgb(36, 224, 255)");
  } finally {
    await a.context.close();
    await b.context.close();
  }
});

test("name claim is unique and the character counter works", async ({ browser }) => {
  const a = await participant(browser, "UniqueName");
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto("/");
    await page.fill("#chat-name-input", "uniquename");
    await page.click("#chat-name-btn");
    await expect(page.locator("#chat-err")).toContainText("taken");
    await expect(page.locator("#chat-input")).toBeDisabled();
    await a.page.fill("#chat-input", "abc");
    await expect(a.page.locator("#chat-count")).toHaveText("3/200");
  } finally {
    await a.context.close();
    await context.close();
  }
});

test("links are rejected and the rate limit is central", async ({ browser }) => {
  const a = await participant(browser, "RateTester");
  try {
    await a.page.fill("#chat-input", "https://example.com");
    await a.page.click("#chat-send");
    await expect(a.page.locator("#chat-err")).toContainText("No links", { timeout: 10000 });

    await a.page.fill("#chat-input", "first message");
    await a.page.click("#chat-send");
    await expect(a.page.locator("#chat-log", { hasText: "first message" })).toBeVisible({ timeout: 10000 });
    await a.page.fill("#chat-input", "second message");
    await a.page.click("#chat-send");
    await expect(a.page.locator("#chat-err")).toContainText("give it a moment", { timeout: 10000 });
  } finally {
    await a.context.close();
  }
});

test("reconnect replays without duplicates", async ({ browser }) => {
  const a = await participant(browser, "ReplayTester");
  try {
    const text = "replay me once";
    await a.page.fill("#chat-input", text);
    await a.page.click("#chat-send");
    await expect(a.page.locator("#chat-log", { hasText: text })).toBeVisible({ timeout: 10000 });
    await a.page.reload();
    await expect(a.page.locator("#chat-status")).toHaveText("GRAZE UPON IT", { timeout: 10000 });
    await expect(a.page.locator("#chat-log .chat-msg", { hasText: text })).toHaveCount(1);
  } finally {
    await a.context.close();
  }
});

test("a deleted message disappears", async ({ browser }) => {
  const a = await participant(browser, "DeleteTester");
  try {
    const text = "delete this message";
    await a.page.fill("#chat-input", text);
    await a.page.click("#chat-send");
    await expect(a.page.locator("#chat-log", { hasText: text })).toBeVisible({ timeout: 10000 });

    const history = await a.page.request.get("/api/chat/history?after=0-0");
    expect(history.ok()).toBeTruthy();
    const body = await history.json();
    const msg = body.messages.find((m: { text: string }) => m.text === text);
    expect(msg).toBeTruthy();
    const resp = await a.page.request.post(`/api/test/chat/delete/${msg.id}`);
    expect(resp.ok()).toBeTruthy();
    await expect(a.page.locator("#chat-log", { hasText: text })).toHaveCount(0, { timeout: 10000 });
  } finally {
    await a.context.close();
  }
});

test("Giles is labelled and signs off exactly once", async ({ page }) => {
  await page.goto("/");
  const resp = await page.request.post("/api/test/chat/giles");
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  expect(body.ok).toBeTruthy();

  const giles = page.locator(".chat-msg.giles");
  await expect(giles).toBeVisible({ timeout: 10000 });
  await expect(giles.locator(".chat-author")).toHaveText("GILES");
  const text = (await giles.locator(".chat-text").textContent()) || "";
  const phrase = "outstanding in my field";
  expect(text.toLowerCase().split(phrase).length - 1).toBe(1);
  expect(text.trim().endsWith(
    "This is Giles signing off with a reminder that I'm outstanding in my field.")).toBeTruthy();
});

test("radio and requests still work beside the pen", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#chat-log")).toBeVisible();
  await page.click("#btn-play");
  await expect(page.locator("#btn-play")).toHaveText(/STOP/i, { timeout: 15000 });
  await page.click("#btn-play");
  await expect(page.locator("#req-queue")).toBeVisible();
  await expect(page.locator("#req-send")).toBeVisible();
});
