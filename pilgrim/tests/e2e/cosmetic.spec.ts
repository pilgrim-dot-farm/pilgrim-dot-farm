/** Cosmetic page tests (COSMETIC_PATCHING). Backend-free; the e2e server seeds
 *  a temp library with fake FLAC + song titles. Verifies pane order/labels,
 *  the song label under the meter, the three-row recent history, the model
 *  credits, hit counter, and infra/data-flow panes, and that a failing visitor counter never
 *  disturbs PLAY or the request line. */
import { test, expect } from "@playwright/test";

test("page layout: pane order, RADIO BROADCAST title, credits + hit counter", async ({ page }) => {
  await page.goto("/");
  const order = await page.$$eval("#desktop > .window", els => els.map(e => e.getAttribute("data-win")));
  expect(order).toEqual(["radio", "requests", "sheep", "about"]);
  expect(await page.textContent('[data-win="radio"] .win-title')).toBe("RADIO BROADCAST");
  expect(await page.textContent('[data-win="about"] .win-title')).toBe("ABOUT");
  // Sub-headings are one style everywhere (feedback 2026-11-08): the ABOUT
  // treatment (shaded box, white, centred) on the request line too.
  const heads = await page.evaluate(() => {
    const pick = (el: Element) => {
      const cs = getComputedStyle(el);
      return { bg: cs.backgroundImage, align: cs.textAlign, size: cs.fontSize,
               color: cs.color, weight: cs.fontWeight };
    };
    return {
      about: pick(document.querySelector(".about-head")!),
      req: pick(document.querySelector(".req-list-head")!),
    };
  });
  expect(heads.req).toEqual(heads.about);
  expect(heads.about.align).toBe("left");
  // the ABOUT rule is inset (never the full box width) and there is no rule
  // above a sub-heading; the head's top and bottom padding are equal
  const rule = await page.evaluate(() => {
    const h = document.querySelector(".about-head")!;
    const cs = getComputedStyle(h);
    const a = getComputedStyle(h, "::after");
    const sub = getComputedStyle(document.querySelector(".about-sub")!);
    const body = h.parentElement!.querySelector(".win-body")!;
    const bcs = getComputedStyle(body);
    const about = getComputedStyle(document.querySelector(".win-body.about")!);
    return { left: a.left, right: a.right, width: a.borderBottomWidth,
             above: sub.borderTopWidth,
             headTop: cs.paddingTop, headBottom: cs.paddingBottom,
             bodyTop: bcs.paddingTop, bodyBottom: bcs.paddingBottom,
             aboutTop: about.paddingTop };
  });
  expect(rule.above).toBe("0px");          // no rule above a sub-heading
  expect(rule.width).toBe("1px");
  expect(rule.left).toBe("24px");          // inset, never edge to edge
  expect(rule.right).toBe("24px");
  // uniform rhythm: 14 px above the text (previous body's bottom padding)
  // and 14 px below it (the head's own bottom padding)
  expect(rule.headTop).toBe("0px");
  expect(rule.headBottom).toBe("14px");
  expect(rule.bodyTop).toBe("14px");
  expect(rule.bodyBottom).toBe("14px");
  expect(rule.aboutTop).toBe("14px");
  // ABOUT heads sit at the same left offset as the request-line heads, and the
  // mission image is exactly as wide as the rule above it (the infra diagram
  // keeps its own 6 px inset)
  const widths = await page.evaluate(() => {
    const off = (sel: string) => {
      const el = document.querySelector(sel)!;
      const body = el.closest(".win-body")!;
      // the head's text starts after its own padding (the request head's
      // padding lives on the body, the ABOUT head's on the head itself)
      return Math.round(el.getBoundingClientRect().left
        + parseFloat(getComputedStyle(el).paddingLeft)
        - body.getBoundingClientRect().left);
    };
    const head = document.querySelector(".about-head")!;
    const a = getComputedStyle(head, "::after");
    const ruleW = head.getBoundingClientRect().width
      - parseFloat(a.left) - parseFloat(a.right);
    const bodyW = document.querySelector(".win-body.about")!.getBoundingClientRect().width;
    return {
      req: off(".req-list-head"), about: off(".about-head"),
      ruleW, bodyW,
      mission: document.querySelector('[data-sub="motivation"] .infra-img')!.getBoundingClientRect().width,
      infra: document.querySelector('[data-sub="infra"] .infra-img')!.getBoundingClientRect().width,
    };
  });
  expect(widths.about).toBe(widths.req);
  expect(Math.abs(widths.mission - widths.ruleW)).toBeLessThan(1);
  expect(Math.abs(widths.infra - (widths.bodyW - 12))).toBeLessThan(1);
  expect(await page.textContent('[data-win="sheep"] .win-title')).toBe("SHEEP PEN");
  const subs = await page.$$eval(".about-sub", els => els.map(e => [e.getAttribute("data-sub"), e.querySelector(".about-head")!.textContent]));
  expect(subs).toEqual([
    ["hit", "HIT COUNTER & SOCIAL"],
    ["motivation", "CORPORATE MISSION STATEMENT"],
    ["infra", "INFRA AND DATA FLOW"],
    ["credits", "CREDITS"],
    ["popculture", "IN POP CULTURE 🌭"],
  ]);
  // eight model rows; the cornucopia asset credit is gone (public domain)
  expect(await page.$$eval(".credits-list li", els => els.length)).toBe(8);
  expect(await page.$$(".cred-attr")).toHaveLength(0);
  // every model links to its HF model card in a new tab
  const links = await page.$$eval(".credits-list a.cred-model",
    els => els.map(a => [a.textContent, a.getAttribute("href"), a.getAttribute("target")]));
  expect(links).toHaveLength(8);
  expect(links[0]).toEqual(["Qwen 3.8 27B", "https://huggingface.co/Qwen/Qwen3.8-27B", "_blank"]);
  expect(links[5]).toEqual(["Kokoro-82M", "https://huggingface.co/hexgrad/Kokoro-82M", "_blank"]);
  for (const [, href] of links) expect(href).toMatch(/^https:\/\/(huggingface\.co|www\.anthropic\.com)\//);
  // IN POP CULTURE: one YouTube embed under CREDITS, scaled to the pane
  const embed = page.locator('[data-sub="popculture"] iframe');
  await expect(embed).toHaveCount(1);
  expect(await embed.getAttribute("src"))
    .toBe("https://www.youtube.com/embed/U49MXakb7f4?si=baZ7zxIQ7y1KWKoP");
  expect(await embed.getAttribute("allowfullscreen")).toBe("");
  const embedBox = await embed.boundingBox();
  const aboutBox = await page.locator('[data-win="about"]').boundingBox();
  expect(embedBox!.width, "the embed fits the 484 px pane").toBeLessThanOrEqual(aboutBox!.width);
  expect(await page.textContent(".visitors")).toMatch(/^Unique visitors:/);
  // HIT COUNTER above it: DB-backed request counts (RADIO §9.1). The e2e
  // server seeds 5 aired + 12 free + 1 paid = 18 rows, one paid.
  await expect(page.locator("#paid-requests")).toHaveText("Paid requests: 1");
  await expect(page.locator("#total-requests")).toHaveText("Total request: 18");
  // the cornucopia marks a paid request: one icon at either end of that line
  const paidIcons = page.locator(".paid-counter .paid-counter-icon");
  await expect(paidIcons).toHaveCount(2);
  for (const icon of await paidIcons.all()) {
    await icon.scrollIntoViewIfNeeded();
    await expect.poll(
      () => icon.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0),
      { timeout: 10_000 }).toBe(true);
    expect(await icon.getAttribute("src")).toBe("img/cornucopia.webp");
    // decorative: the text already says "Paid requests"
    expect(await icon.getAttribute("alt")).toBe("");
  }
  // icons and text share one line height: each icon's vertical centre sits
  // within the text's box (same height at either end)
  const textBox = await page.locator("#paid-requests").boundingBox();
  for (const icon of await paidIcons.all()) {
    const box = await icon.boundingBox();
    const centre = box!.y + box!.height / 2;
    expect(centre).toBeGreaterThan(textBox!.y);
    expect(centre).toBeLessThan(textBox!.y + textBox!.height);
  }
  await expect(page.locator("#listeners")).toHaveText("Listening now: 68.10");
  // the hit-counter pane carries the social links as icons (cosmetic patch):
  // Instagram / Reddit / GitHub, each an SVG glyph in a new tab
  const socials = await page.$$eval(".social-row a", els => els.map(a => {
    const img = a.querySelector("img") as HTMLImageElement | null;
    return [a.getAttribute("href"), a.getAttribute("target"), a.getAttribute("rel"),
            img ? img.getAttribute("src") : null, img ? img.getAttribute("alt") : null];
  }));
  expect(socials).toEqual([
    ["https://www.instagram.com/pilgrim.farm/", "_blank", "noopener noreferrer",
     "img/instagram.svg", "Instagram"],
    ["https://www.reddit.com/r/pilgrimdotfarm", "_blank", "noopener noreferrer",
     "img/reddit.svg", "Reddit"],
    ["https://github.com/pilgrim-dot-farm/pilgrim-dot-farm", "_blank", "noopener noreferrer",
     "img/github.svg", "GitHub"],
  ]);
  // the old text links are gone
  expect(await page.$$(".hit-link")).toHaveLength(0);
  // every icon is an SVG that actually loads, rendered 40-48 px wide
  for (const img of await page.locator(".social-row img").all()) {
    await img.scrollIntoViewIfNeeded();
    await expect.poll(
      () => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0),
      { timeout: 10_000 }).toBe(true);
    const box = await img.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(40);
    expect(box!.width).toBeLessThanOrEqual(48);
    const src = await img.getAttribute("src");
    const r = await page.request.get("/" + src);
    expect(r.ok()).toBeTruthy();
    expect(r.headers()["content-type"]).toContain("image/svg+xml");
  }
  // the infra pane shows infra.png as content, and the image is not a link
  // (feedback 2026-11-08: tapping an image must not open it)
  const infra = await page.$eval('[data-sub="infra"] .infra-img',
    img => [img.getAttribute('src'), img.getAttribute('alt'),
            img.closest("a") ? "link" : "plain"]);
  expect(infra[0]).toBe("img/infra.png");
  expect(infra[1]).toMatch(/infrastructure and data flow/i);
  expect(infra[2]).toBe("plain");
  expect(await page.$$('[data-sub="infra"] a, [data-sub="motivation"] a')).toHaveLength(0);
  // and the asset actually loads (not a 404 placeholder)
  // (loading="lazy" and below the fold since MOTIVATION: scroll to it first)
  await page.locator('[data-sub="infra"] .infra-img').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => {
    const img = document.querySelector('[data-sub="infra"] .infra-img') as HTMLImageElement | null;
    return !!img && img.complete && img.naturalWidth > 0;
  }, null, { timeout: 10_000 });
  const loaded = await page.$eval('[data-sub="infra"] .infra-img', img => img.complete && img.naturalWidth > 0);
  expect(loaded).toBe(true);
  // COSMETIC_PATCHING §7: the diagram is scaled to the pane's INTERIOR width
  // (pane width minus borders and .win-body padding), never its intrinsic
  // 1024px (which .window{overflow:hidden} would clip).
  const box = await page.$eval('[data-sub="infra"] .infra-img', img => {
    const r = img.getBoundingClientRect();
    const body = img.closest('.win-body') as HTMLElement;
    const cs = getComputedStyle(body);
    const interior = body.clientWidth
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    return { imgW: r.width, imgH: r.height, interior, natW: img.naturalWidth,
             natH: img.naturalHeight, contentW: img.clientWidth, contentH: img.clientHeight };
  });
  // border-box width equals the pane's interior width (border-box sizing is global)
  expect(Math.abs(box.imgW - box.interior)).toBeLessThan(1);
  expect(box.imgW).toBeLessThan(box.natW);
  // height follows the width, preserving the diagram's aspect ratio (content box)
  expect(Math.abs(box.contentH - box.contentW * (box.natH / box.natW))).toBeLessThan(1);
});

test("song label shows the audible track, not a buffered next one; STOP clears it", async ({ page }) => {
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  // wait for an actual song to go on air (label driven by the audio clock)
  await page.waitForFunction(
    () => (document.getElementById("song-label") || {}).textContent?.length > 0,
    null, { timeout: 20_000 });
  // title on its own line, artist below it (RADIO §9.1)
  expect(await page.textContent("#song-title")).toMatch(/^Song \d+$/);
  expect(await page.textContent("#song-artist")).toBe("The Fakes");
  const label = await page.textContent("#song-label");
  // label is set via textContent (safe rendering) and never shows 'undefined'/'null'
  expect(label).not.toMatch(/undefined|null|-/);
  const [titleFs, artistFs] = await page.evaluate(() => [
    parseFloat(getComputedStyle(document.getElementById("song-title")!).fontSize),
    parseFloat(getComputedStyle(document.getElementById("song-artist")!).fontSize)]);
  expect(titleFs - artistFs).toBe(4);
  // STOP clears the label (also covers cross-tab STOP path)
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/idle/);
  expect(await page.textContent("#song-label")).toBe("");
});

test("aired requests table shows up to five with a prep time", async ({ page }) => {
  await page.goto("/");
  await page.fill("#req-input", "play the synthesizer, Liam");
  await page.click("#req-send");
  await expect(page.locator(".req-recent-item")).toHaveCount(5, { timeout: 10_000 });
  const waits = await page.$$eval(".req-wait", els => els.map(e => e.textContent));
  expect(waits.length).toBeGreaterThan(0);
  expect(waits[0]).toMatch(/^\d+ (min|h|d)/);   // column is headed "Prep Time"
  // an unmeasured row (wait_s null) reads as an em dash, not a blank cell
  expect(waits[4]).toBe("—");
  const airedTable = page.locator(".req-table").filter({ has: page.locator("#req-recent") });
  await expect(airedTable.getByRole("columnheader")).toHaveText(["Prompt", "Title", "Prep Time"]);
  // Prompt and Title are the same width: each half of the space left over
  // after Prep Time's fixed slice (cosmetic parity request)
  const widths = await airedTable.evaluate((table) => {
    const ths = Array.from(table.querySelectorAll("thead th"));
    const px = (th) => th.getBoundingClientRect().width;
    return { prompt: px(ths[0]), title: px(ths[1]), time: px(ths[2]), total: table.getBoundingClientRect().width };
  });
  expect(Math.abs(widths.prompt - widths.title)).toBeLessThanOrEqual(1);
  expect(widths.prompt / widths.total).toBeCloseTo(0.43, 1);
  // the queue's vote column is left-most (cosmetic patch); no Title column
  // (RADIO §9.1) and the box is 520 px tall
  const queueTable = page.locator(".req-table").filter({ has: page.locator("#req-queue") });
  await expect(queueTable.getByRole("columnheader"))
    .toHaveText(["Votes", "Request", "Status"]);
  expect(await page.$eval(".req-scroll", el => getComputedStyle(el).maxHeight)).toBe("520px");
  expect(await page.$eval("#req-queue tr.req-item td:first-child",
    td => td.classList.contains("req-vote"))).toBe(true);
  // the queue table's header row stays pinned while its box scrolls
  const pos = await page.$eval(".req-scroll thead th",
                               el => getComputedStyle(el).position);
  expect(pos).toBe("sticky");
  // Mobile (≤520 px): the Prep Time column keeps its header and a width of
  // at least 3 ch so "min" never breaks one character per line; the queue's
  // Status column still folds under the request text; nothing scrolls horizontally.
  await page.setViewportSize({ width: 360, height: 420 });
  await page.click('#nav-mobile [data-go="requests"]');
  await expect(airedTable.getByRole("columnheader", { name: "Prep Time" }))
    .toBeVisible();
  // Prep Time is right-aligned, header and value (feedback 2026-11-08)
  const prepAlign = await airedTable.evaluate((table) => ({
    header: getComputedStyle(table.querySelector("thead .c-time")!).textAlign,
    cell: getComputedStyle(table.querySelector("td.req-wait")!).textAlign,
  }));
  expect(prepAlign.header).toBe("right");
  expect(prepAlign.cell).toBe("right");
  // the AIRED REQUESTS table header row is flush with the pink section rule
  const flush = await page.evaluate(() => {
    const heads = document.querySelectorAll('[data-win="requests"] .req-list-head');
    const head = heads[heads.length - 1] as HTMLElement;
    const table = head.nextElementSibling as HTMLElement;
    const hb = head.getBoundingClientRect();
    const th = table.querySelector("thead th")!.getBoundingClientRect();
    return { gap: th.top - hb.bottom };
  });
  expect(flush.gap).toBeLessThanOrEqual(1);
  const prep = await airedTable.evaluate((table) => {
    const ths = Array.from(table.querySelectorAll("thead th"));
    const time = ths[2];
    const probe = document.createElement("span");
    probe.style.cssText = `position: absolute; visibility: hidden;
      font: ${getComputedStyle(time).font};`;
    probe.textContent = "0";   // 1 ch measured against the header's font
    document.body.appendChild(probe);
    const ch = probe.getBoundingClientRect().width;
    probe.remove();
    const cell = Array.from(table.querySelectorAll("td.req-wait"))
      .find((e) => /min/.test(e.textContent || ""));
    let minOneLine = true;
    if (cell && cell.firstChild) {
      const txt = cell.textContent || "";
      const i = txt.indexOf("min");
      const r = document.createRange();
      r.setStart(cell.firstChild, i);
      r.setEnd(cell.firstChild, i + 3);
      // a single character never spans two line boxes, so its rect height is
      // exactly one line box (line-height can compute to "normal"/non-numeric)
      const one = document.createRange();
      one.setStart(cell.firstChild, 0);
      one.setEnd(cell.firstChild, 1);
      const lineH = one.getBoundingClientRect().height;
      // a mid-word break of "min" spans two line boxes; one fits in 1.5 of it
      minOneLine = r.getBoundingClientRect().height <= lineH * 1.5;
    }
    return {
      width: time.getBoundingClientRect().width,
      ch,
      minOneLine,
      overflow: table.scrollWidth - table.clientWidth,
    };
  });
  expect(prep.width).toBeGreaterThanOrEqual(3 * prep.ch);
  expect(prep.minOneLine).toBe(true);
  expect(prep.overflow).toBeLessThanOrEqual(1);
  // all queue columns stay visible on mobile (feedback 2026-11-08): Vote /
  // Request / Status; the old inline status fallback is gone.
  const queueStatus = await page.$eval(
    ".req-table:not(.req-table-aired) thead .c-time",
                                        el => getComputedStyle(el).display);
  expect(queueStatus).toBe("table-cell");
  await expect(page.locator(".req-item .req-status").first()).toBeVisible();
  await expect(page.locator(".req-item .req-status-inline")).toHaveCount(0);
  // a paid row (not votable) shows an em dash, not an empty vote cell
  await expect(page.locator(".req-item .req-vote.req-dash").first()).toHaveText("—");
});

test("a failed visitor counter never breaks PLAY or request submission", async ({ page }) => {
  await page.route("**/api/visitors", route => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ detail: "unavailable" }) }));
  await page.goto("/");
  // counter falls back to 0, never a hyphen
  await expect(page.locator("#visitors")).toHaveText("Unique visitors: 0");
  await expect(page.locator("#paid-requests")).toHaveText("Paid requests: 0");
  await expect(page.locator("#total-requests")).toHaveText("Total request: 0");
  // PLAY still works
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  await expect(page.locator("#btn-play")).toContainText("STOP");
  // request submission still works
  await page.fill("#req-input", "another tune, please");
  await page.click("#req-send");
  await expect(page.locator(".req-recent-item")).toHaveCount(5, { timeout: 10_000 });
});

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
