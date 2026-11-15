import { expect, test, type Page } from "@playwright/test";

/* docs/shipped/prompt-upgrade.md Step 5 (RADIO §9.1): the paid request panel is three
   creative departments — LYRICS / MUSICAL DIRECTION / COVER ART — with the
   technical help collapsed, no model jargon in the main form, drafts that
   survive a reload, and a `{lyrics, music, art}` POST. The e2e server accepts
   the three-box body, validates it like the real route, stores nothing and
   answers with a fake rejection, so the client returns to composing. */

const LYRICS = "A raccoon becomes president of the moon.";
const MUSIC = "Death metal.";

async function openPanel(page: Page) {
  await page.goto("/");
  const tab = page.locator('#nav-mobile [data-go="requests"]');
  if (await tab.isVisible()) await tab.click();
  await expect(page.locator("#pay-panel")).toBeVisible();
}

/* The departments show one at a time (2026-11-15): walk NEXT to step `n`
   (1-4) from the current one. NEXT refuses to leave an empty required box, so the helper fills
   LYRICS and MUSICAL DIRECTION on the way unless they already hold text. */
async function gotoStep(page: Page, n: number) {
  const required = [["#pay-lyrics", LYRICS], ["#pay-music", MUSIC]];
  const at = Number((await page.locator("#pay-step-at").textContent())!.split(" ")[0]);
  for (let i = at; i < n; i++) {
    await expect(page.locator("#pay-step-at")).toHaveText(`${i} / 4`);
    const req = required[i - 1];
    if (req && !(await page.locator(req[0]).inputValue())) {
      await page.fill(req[0], req[1]);
    }
    await page.click("#pay-next");
  }
  await expect(page.locator("#pay-step-at")).toHaveText(`${n} / 4`);
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
  const promo = page.locator("#pay-promo");
  await expect(lyrics).toHaveAttribute("maxlength", "4000");
  await expect(music).toHaveAttribute("maxlength", "3000");
  await expect(art).toHaveAttribute("maxlength", "800");

  // One department at a time, in the numbered order; PREV/NEXT walk them and
  // CHECK IT & PAY replaces NEXT on the last step only (2026-11-15).
  const all = [lyrics, music, art, promo];
  const onlyVisible = async (at: number) => {
    for (let i = 0; i < all.length; i++) {
      if (i === at) await expect(all[i]).toBeVisible();
      else await expect(all[i]).toBeHidden();
    }
  };
  await onlyVisible(0);
  await expect(page.locator("#pay-prev")).toBeDisabled();
  await expect(page.locator("#pay-next")).toBeVisible();
  await expect(page.locator("#pay-go")).toBeHidden();
  // offset inside the panel (the page may scroll between clicks)
  const offset = async (sel: string) =>
    (await page.locator(sel).boundingBox())!.y
    - (await page.locator("#pay-panel").boundingBox())!.y;
  const top = await offset("#pay-next");
  await gotoStep(page, 2);
  await onlyVisible(1);
  await expect(page.locator("#pay-go")).toBeHidden();
  await gotoStep(page, 3);
  await onlyVisible(2);
  // the buttons do not move between steps
  expect(await offset("#pay-next")).toBeCloseTo(top, 1);
  await page.click("#pay-next");
  await onlyVisible(3);
  await expect(page.locator("#pay-next")).toBeHidden();
  await expect(page.locator("#pay-go")).toBeVisible();
  expect(await offset("#pay-go")).toBeCloseTo(top, 1);
  await page.click("#pay-prev");
  await onlyVisible(2);
  await expect(page.locator("#pay-go")).toBeHidden();

  await expect(page.locator("label[for=pay-lyrics]")).toContainText("LYRICS");
  await expect(page.locator("label[for=pay-music]")).toContainText("MUSICAL DIRECTION");
  await expect(page.locator("label[for=pay-art]")).toContainText("COVER ART");
  // PROMO CODE matches the optional COVER ART heading, and its one-line
  // description is the short version (feedback 2026-11-08)
  await expect(page.locator("label[for=pay-promo]")).toContainText("PROMO CODE (Optional)");
  await expect(page.locator(".pay-field-promo .pay-help"))
    .toHaveText("Got a code? Ride for free!");
  // no example text in the priority boxes (too much reading)
  for (const id of ["#pay-lyrics", "#pay-music", "#pay-art"]) {
    await expect(page.locator(id)).not.toHaveAttribute("placeholder", /./);
  }

  // the help and the terms sit above the form, under the heading
  const help = page.locator(".pay-advanced");
  await expect(help).not.toHaveAttribute("open", "");
  await expect(help.locator("summary")).toHaveText("Want to get technical?");
  const helpY = (await help.boundingBox())!.y;
  expect(helpY).toBeLessThan((await page.locator("#pay-panel").boundingBox())!.y);
  await expect(page.locator(".pay-terms")).toContainText("$2.50 minimum");
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
  await page.click(".pay-advanced summary");
  const full = (await page.locator(".pay-advanced").textContent())!.toLowerCase();
  expect(full).toContain("minimax");
});

test("departments 1 and 2 are both required before anything is posted", async ({ page }) => {
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

  // NEXT refuses to leave an empty required box.
  expect(await requestSeen(() => page.click("#pay-next"))).toBe(false);
  await expect(page.locator("#pay-msg")).toContainText("LYRICS are required.");
  await expect(page.locator("#pay-step-at")).toHaveText("1 / 4");
  await page.fill("#pay-lyrics", "   ");
  await page.click("#pay-next");
  await expect(page.locator("#pay-step-at")).toHaveText("1 / 4");

  await page.fill("#pay-lyrics", LYRICS);
  await page.click("#pay-next");
  await expect(page.locator("#pay-step-at")).toHaveText("2 / 4");
  await expect(page.locator("#pay-msg")).toBeHidden();
  expect(await requestSeen(() => page.click("#pay-next"))).toBe(false);
  await expect(page.locator("#pay-msg")).toContainText(
    "MUSICAL DIRECTION is required.");
  await expect(page.locator("#pay-step-at")).toHaveText("2 / 4");

  // Going back and emptying a box closes the way forward again.
  await page.fill("#pay-music", MUSIC);
  await page.click("#pay-prev");
  await page.fill("#pay-lyrics", "");
  await page.click("#pay-next");
  await expect(page.locator("#pay-step-at")).toHaveText("1 / 4");
});

test("the pay button still checks both required boxes", async ({ page }) => {
  await openPanel(page);
  // A resumed request opens on the last step, where the boxes may be empty:
  // the button must not post, and it walks back to the empty department.
  await page.evaluate(() => {
    sessionStorage.setItem("pd_pay_token", JSON.stringify({
      token: "stale-token-654321", ts: Date.now(),
    }));
  });
  await page.route("**/api/paid-requests/stale-token-654321", (r) => r.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ status: "awaiting_payment" }),
  }));
  await page.reload();
  await expect(page.locator("#pay-step-at")).toHaveText("4 / 4");
  let posted = false;
  page.on("request", (req) => {
    if (req.url().endsWith("/api/paid-requests") && req.method() === "POST") posted = true;
  });
  await page.click("#pay-go");
  await expect(page.locator("#pay-msg")).toContainText(
    "Both LYRICS and MUSICAL DIRECTION are required");
  await expect(page.locator("#pay-step-at")).toHaveText("1 / 4");
  await expect(page.locator("#pay-lyrics")).toBeVisible();
  await page.waitForTimeout(300);
  expect(posted).toBe(false);
});

test("a three-box submit posts exactly {lyrics, music, art}", async ({ page }) => {
  await openPanel(page);
  await gotoStep(page, 4);

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

test("drafts survive a reload; there is no Cancel button", async ({ page }) => {
  await openPanel(page);
  await gotoStep(page, 3);
  await page.fill("#pay-art", "a chrome vending machine");

  await page.reload();
  // a reload starts over at step 1 with the boxes restored
  await expect(page.locator("#pay-step-at")).toHaveText("1 / 4");
  await expect(page.locator("#pay-lyrics")).toHaveValue(LYRICS);
  await expect(page.locator("#pay-music")).toHaveValue(MUSIC);
  await expect(page.locator("#pay-art")).toHaveValue("a chrome vending machine");
  // the payment UI closes itself once done; Cancel was removed (2026-11-08)
  await expect(page.locator("#pay-cancel")).toHaveCount(0);
});

test("the priority textareas use the description font size", async ({ page }) => {
  await openPanel(page);
  const sizes = await page.evaluate(() => {
    const help = parseFloat(getComputedStyle(
      document.querySelector("#pay-panel .pay-help")!).fontSize);
    return [...document.querySelectorAll("#pay-panel textarea")]
      .map((t) => parseFloat(getComputedStyle(t).fontSize)).concat(help);
  });
  for (const s of sizes) expect(s).toBe(13);
});

test("a spent promo code says it is all used up", async ({ page }) => {
  await openPanel(page);
  await gotoStep(page, 4);
  await page.fill("#pay-promo", "SPENT-CODE");
  await page.click("#pay-promo-apply");
  await expect(page.locator("#pay-promo-msg"))
    .toHaveText("That code is all used up.");
  await expect(page.locator("#pay-promo-msg")).toHaveClass(/err/);
});

test("a live promo code applies and spends its use on submit", async ({ page }) => {
  await openPanel(page);
  await gotoStep(page, 4);
  await page.fill("#pay-promo", "RIDE-FREE");
  await page.click("#pay-promo-apply");
  await expect(page.locator("#pay-promo-msg"))
    .toHaveText("Success! This one's on us");
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/api/paid-requests")
      && r.method() === "POST"),
    page.click("#pay-go"),
  ]);
  const body = request.postDataJSON() as Record<string, string>;
  expect(body.promo).toBe("RIDE-FREE");
  expect(body.voter_id).toMatch(/^[0-9a-f]{32}$/);
  // the fake e2e server still rejects the request, but the use was spent
  await expect(page.locator("#pay-msg")).toContainText(
    "e2e: payments are a fake here");
});

test("promo code: two wrong tries grey it out; a valid code goes green", async ({ page }) => {
  await openPanel(page);
  // fourth department, grey (feedback 2026-11-08)
  await gotoStep(page, 4);
  await expect(page.locator(".pay-field-promo")).toBeVisible();
  await expect(page.locator("#pay-promo-apply")).toHaveText("APPLY");
  // no reserved blank line under the input while there is no message
  await expect(page.locator("#pay-promo-msg")).toBeHidden();
  const [grey, dim] = await page.evaluate(() => [
    getComputedStyle(document.querySelector(".pay-field-promo")!)
      .getPropertyValue("--field-color").trim(),
    getComputedStyle(document.documentElement).getPropertyValue("--dim").trim(),
  ]);
  expect(grey).toBe(dim);

  // the fakes server has no comp secret: mock the validation endpoint
  await page.route("**/api/paid-requests/promo", async (route) => {
    const body = route.request().postDataJSON() as { code: string };
    await route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: body.code === "GOOD-CODE" }) });
  });

  await page.fill("#pay-promo", "BAD1");
  await page.click("#pay-promo-apply");
  await expect(page.locator("#pay-promo-msg")).toHaveText("FAILED: 1 attempts remain.");
  await expect(page.locator("#pay-promo-msg")).toHaveClass(/err/);

  await page.fill("#pay-promo", "BAD2");
  await page.click("#pay-promo-apply");
  await expect(page.locator("#pay-promo-msg")).toHaveText("FAILED: no attempts remain.");
  await expect(page.locator("#pay-promo")).toBeDisabled();
  await expect(page.locator("#pay-promo-apply")).toBeDisabled();

  // the counter is client-side: a reload gives two fresh attempts
  await page.reload();
  await gotoStep(page, 4);
  await expect(page.locator("#pay-promo")).toBeEnabled();
  await page.fill("#pay-promo", "GOOD-CODE");
  await page.click("#pay-promo-apply");
  await expect(page.locator("#pay-promo-msg")).toHaveText("Success! This one's on us");
  await expect(page.locator("#pay-promo-msg")).toHaveClass(/ok/);
  await expect(page.locator("#pay-promo")).toBeDisabled();
  await expect(page.locator("#pay-promo-apply")).toBeDisabled();
});

test("at 360x740 the one-step panel fits in under 300 px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await openPanel(page);
  const box = await page.locator("#pay-panel").boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeLessThan(300);
});

test("a stale checkout token does not lock the form on reload", async ({ page }) => {
  await openPanel(page);
  // a listener who opened Stripe and walked away keeps a saved token; on the
  // next visit it must not freeze the whole panel (the "button does nothing"
  // report, 2026-11-08)
  await page.evaluate(() => {
    sessionStorage.setItem("pd_pay_token", JSON.stringify({
      token: "stale-token-123456", ts: Date.now(),
    }));
  });
  await page.route("**/api/paid-requests/stale-token-123456", (r) => r.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ status: "awaiting_payment" }),
  }));
  await page.reload();
  await expect(page.locator("#pay-lyrics")).toBeEnabled();
  await expect(page.locator("#pay-music")).toBeEnabled();
  await expect(page.locator("#pay-go")).toBeEnabled();
  await expect(page.locator("#pay-msg")).toContainText(
    "last checkout wasn't finished");
  await expect(page.locator("#pay-msg")).toContainText("still in the line");
});
