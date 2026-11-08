import { expect, test, type Page } from "@playwright/test";

/* PROMPT_UPGRADE.md Step 5 (RADIO §9.1): the paid request panel is three
   creative departments — LYRICS / MUSICAL DIRECTION / COVER ART — with the
   technical help collapsed, no model jargon in the main form, drafts that
   survive a reload, and a `{lyrics, music, art}` POST. The e2e server accepts
   the three-box body, validates it like the real route, stores nothing and
   answers with a fake rejection, so the client returns to composing. */

const LYRICS = "A raccoon becomes president of the moon.";
const MUSIC = "Death metal.";

async function openPanel(page: Page) {
  await page.goto("/");
  await expect(page.locator("#pay-panel")).toBeVisible();
}

function panelMainText(page: Page) {
  return page.locator("#pay-panel").evaluate((panel) => {
    const clone = panel.cloneNode(true) as HTMLElement;
    clone.querySelectorAll(".pay-advanced").forEach((el) => el.remove());
    return clone.textContent || "";
  });
}

test("the panel shows the three numbered departments, collapsed help and the CTA", async ({ page }) => {
  await openPanel(page);

  const lyrics = page.locator("#pay-lyrics");
  const music = page.locator("#pay-music");
  const art = page.locator("#pay-art");
  await expect(lyrics).toBeVisible();
  await expect(music).toBeVisible();
  await expect(art).toBeVisible();
  await expect(lyrics).toHaveAttribute("maxlength", "4000");
  await expect(music).toHaveAttribute("maxlength", "3000");
  await expect(art).toHaveAttribute("maxlength", "800");

  // DOM order is the numbered order, top to bottom.
  const ys = await Promise.all([lyrics, music, art].map(async (l) =>
    (await l.boundingBox())!.y));
  expect(ys[0]).toBeLessThan(ys[1]);
  expect(ys[1]).toBeLessThan(ys[2]);

  await expect(page.locator("label[for=pay-lyrics]")).toContainText("LYRICS");
  await expect(page.locator("label[for=pay-music]")).toContainText("MUSICAL DIRECTION");
  await expect(page.locator("label[for=pay-art]")).toContainText("COVER ART");

  const help = page.locator("#pay-panel .pay-advanced");
  await expect(help).not.toHaveAttribute("open", "");
  await expect(help.locator("summary")).toHaveText("Want to get technical?");
  await expect(page.locator("#pay-go")).toHaveText("CHECK IT & PAY");
});

test("the main form carries no model or prompt jargon", async ({ page }) => {
  await openPanel(page);
  const text = (await panelMainText(page)).toLowerCase();
  for (const word of ["minimax", "flux", "llm", "prompt", "caption", "token",
                      "metadata"]) {
    expect(text, `the main form must not say ${word}`).not.toContain(word);
  }
  // The technical help is where those words are allowed.
  await page.click("#pay-panel .pay-advanced summary");
  const full = (await page.locator("#pay-panel").textContent())!.toLowerCase();
  expect(full).toContain("minimax");
});

test("empty and art-only submits stay local and explain why", async ({ page }) => {
  await openPanel(page);

  const requestSeen = async (fn: () => Promise<void>) => {
    let posted = false;
    const listener = (req: { url: () => string }) => {
      if (req.url().includes("/api/paid-requests")) posted = true;
    };
    page.on("request", listener);
    await fn();
    await page.waitForTimeout(300);
    page.off("request", listener);
    return posted;
  };

  expect(await requestSeen(() => page.click("#pay-go"))).toBe(false);
  await expect(page.locator("#pay-msg")).toContainText(
    "Give us a subject or a sound");

  await page.fill("#pay-art", "a moonlit cliff");
  expect(await requestSeen(() => page.click("#pay-go"))).toBe(false);
  await expect(page.locator("#pay-msg")).toContainText(
    "Give us a subject or a sound");
});

test("a three-box submit posts exactly {lyrics, music, art}", async ({ page }) => {
  await openPanel(page);
  await page.fill("#pay-lyrics", LYRICS);
  await page.fill("#pay-music", MUSIC);

  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/api/paid-requests")
      && r.method() === "POST"),
    page.click("#pay-go"),
  ]);
  expect(request.postDataJSON()).toEqual({
    lyrics: LYRICS, music: MUSIC, art: "",
  });
  await expect(page.locator("#pay-msg")).toContainText(
    "e2e: payments are a fake here");
  // Back to composing: the boxes are editable again.
  await expect(page.locator("#pay-lyrics")).toBeEnabled();
  await expect(page.locator("#pay-go")).toBeEnabled();
});

test("drafts survive a reload and Cancel clears them", async ({ page }) => {
  await openPanel(page);
  await page.fill("#pay-lyrics", LYRICS);
  await page.fill("#pay-music", MUSIC);
  await page.fill("#pay-art", "a chrome vending machine");

  await page.reload();
  await expect(page.locator("#pay-lyrics")).toHaveValue(LYRICS);
  await expect(page.locator("#pay-music")).toHaveValue(MUSIC);
  await expect(page.locator("#pay-art")).toHaveValue("a chrome vending machine");

  await page.click("#pay-cancel");
  await expect(page.locator("#pay-panel")).toBeVisible();
  await expect(page.locator("#pay-lyrics")).toHaveValue("");
  await expect(page.locator("#pay-music")).toHaveValue("");
  await expect(page.locator("#pay-art")).toHaveValue("");
});

test("at 360x740 the empty panel fits in under 1,100 px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await openPanel(page);
  const box = await page.locator("#pay-panel").boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeLessThan(1100);
});
