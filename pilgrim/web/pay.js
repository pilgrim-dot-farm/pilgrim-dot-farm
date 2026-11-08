/* Pilgrim Dot Farm — PAID REQUE$T (PAYMENT.md §8.3, Step 12b;
   PROMPT_UPGRADE.md Step 5, RADIO §9.1).
   Three creative departments (lyrics, musical direction, cover art) posted as
   {lyrics, music, art}. Submit -> the station checks the composed request in
   stages (DJ prefilter, content moderation, copied-lyrics screen) while this
   polls the stage -> Embedded Stripe Checkout once the request is ready.
   Stripe.js is loaded lazily; the card fields live in Stripe's iframe, so card
   data never touches this code or the station. Drafts survive a reload in
   sessionStorage. No build step.
*/
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const btn = $("pay-open"), panel = $("pay-panel");
  const taLyrics = $("pay-lyrics"), taMusic = $("pay-music"), taArt = $("pay-art");
  const advanced = panel ? panel.querySelector(".pay-advanced") : null;
  const boxes = [taLyrics, taMusic, taArt].filter(Boolean);
  const go = $("pay-go"), cancel = $("pay-cancel"), msg = $("pay-msg");
  const mount = $("pay-checkout");
  if (!btn || !panel || !mount || boxes.length !== 3) return;

  const LABEL = "PRIORITY REQUEST\n($3 MIN)";
  const STORE_KEY = "pd_pay_token";
  const STORE_TTL_MS = 10 * 60 * 1000;
  const DRAFT_KEY = "pd_pay_draft";
  const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

  let state = "idle";
  let checkout = null;
  let clientSecret = null;
  let publishableKey = null;
  let token = null;
  let pollTimer = null;
  let doneTimer = null;
  let pollStarted = 0;
  let stripePromise = null;

  function showMsg(text, ok) {
    msg.textContent = text || "";
    msg.classList.toggle("hidden", !text);
    msg.style.color = ok ? "var(--ok)" : "var(--warn)";
  }

  function setState(next) {
    state = next;
    render();
  }

  function render() {
    panel.classList.toggle("hidden", state === "idle");
    btn.classList.toggle("hidden", state !== "idle");
    btn.disabled = state !== "idle";
    const locked = state === "checking" || state === "paying" || state === "confirming";
    for (const ta of boxes) ta.disabled = locked;
    if (advanced) advanced.open = false;
    go.disabled = locked;
    cancel.disabled = state === "checking" || state === "confirming";
    if (state === "idle") {
      mount.textContent = "";
      showMsg("");
    }
  }

  function clearTimers() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (doneTimer) { clearTimeout(doneTimer); doneTimer = null; }
  }

  function destroyCheckout() {
    if (checkout) {
      try { checkout.destroy(); } catch (e) { /* already gone */ }
      checkout = null;
    }
    mount.textContent = "";
  }

  function saveDraft() {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
        lyrics: taLyrics.value, music: taMusic.value, art: taArt.value,
        ts: Date.now(),
      }));
    } catch (e) { /* private mode */ }
  }

  function clearDraft() {
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ }
    for (const ta of boxes) ta.value = "";
  }

  function loadDraft() {
    try {
      const raw = sessionStorage.getItem(DRAFT_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!saved || Date.now() - saved.ts > DRAFT_TTL_MS) return;
      taLyrics.value = saved.lyrics || "";
      taMusic.value = saved.music || "";
      taArt.value = saved.art || "";
    } catch (e) { /* private mode / bad JSON */ }
  }

  function saveToken(tok) {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ token: tok, ts: Date.now() }));
    } catch (e) { /* private mode */ }
  }

  function loadToken() {
    try {
      const raw = sessionStorage.getItem(STORE_KEY);
      if (!raw) return null;
      const saved = JSON.parse(raw);
      if (!saved || !saved.token || Date.now() - saved.ts > STORE_TTL_MS) return null;
      return saved.token;
    } catch (e) { return null; }
  }

  function clearToken() {
    try { sessionStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ }
  }

  function loadStripe(pk) {
    if (stripePromise) return stripePromise;
    stripePromise = new Promise((resolve, reject) => {
      const start = () => {
        if (typeof window.Stripe !== "function") { reject(new Error("no Stripe")); return; }
        resolve(window.Stripe(pk));
      };
      if (typeof window.Stripe === "function") { start(); return; }
      const script = document.createElement("script");
      script.src = "https://js.stripe.com/v3/";
      script.async = true;
      script.onload = start;
      script.onerror = () => reject(new Error("stripe load failed"));
      document.head.appendChild(script);
    });
    return stripePromise;
  }

  async function poll() {
    if (!token) return;
    try {
      const r = await fetch("/api/paid-requests/" + encodeURIComponent(token));
      if (!r.ok) return;
      const d = await r.json();
      if (["queued", "producing", "ready", "aired"].includes(d.status)) {
        clearTimers();
        clearToken();
        const pos = d.position ? " (#" + d.position + ")" : "";
        showMsg("✓ You're in the priority line" + pos + ".", true);
        clearDraft();
        setState("done");
        doneTimer = setTimeout(() => setState("idle"), 8000);
        return;
      }
      if (d.status === "failed" || d.status === "abandoned" || d.status === "rejected") {
        clearTimers();
        clearToken();
        showMsg("That request didn't make it into the line. If you were charged, we'll refund it.", false);
        setState("done-pending");
        doneTimer = setTimeout(() => setState("idle"), 8000);
        return;
      }
    } catch (e) { /* station down: keep polling */ }
    if (Date.now() - pollStarted > 90000) {
      clearTimers();
      clearToken();
      showMsg("Payment received. Your request will appear shortly.", true);
      setState("done-pending");
      doneTimer = setTimeout(() => setState("idle"), 8000);
    }
  }

  const STAGE_MSG = {
    moderation: "Checking for unseemly content…",
    copyright: "Checking for existing copyrighted material…",
  };
  const CHECK_TIMEOUT_MS = 120000;

  function backToComposing(text) {
    clearTimers();
    clearToken();
    token = null;
    showMsg(text, false);
    setState("composing");
  }

  let checkoutAsked = false;

  async function checkPoll() {
    if (!token || state !== "checking" || checkoutAsked) return;
    let d = null;
    try {
      const r = await fetch("/api/paid-requests/" + encodeURIComponent(token));
      if (r.ok) d = await r.json();
    } catch (e) { /* station down: keep polling */ }
    // a slower earlier poll must not act after checkout started or the user left
    if (state !== "checking" || checkoutAsked) return;
    if (d) {
      if (d.status === "checking") {
        if (d.stage === "ready") {
          checkoutAsked = true;
          clearTimers();
          await requestCheckout();
          return;
        }
        if (STAGE_MSG[d.stage]) showMsg(STAGE_MSG[d.stage], true);
      } else if (d.status === "rejected") {
        backToComposing("Head's up: " + (d.reason || "that didn't pass the DJ's taste filter."));
        return;
      } else if (d.status === "abandoned" || d.status === "failed") {
        backToComposing("Couldn't finish checking your request. You were not charged; try again.");
        return;
      } else if (d.status === "awaiting_payment") {
        startConfirming(token);   // resumed after a reload mid-checkout
        return;
      } else if (["queued", "producing", "ready", "aired"].includes(d.status)) {
        clearTimers();
        clearToken();
        const pos = d.position ? " (#" + d.position + ")" : "";
        showMsg("✓ You're in the priority line" + pos + ".", true);
        clearDraft();
        setState("done");
        doneTimer = setTimeout(() => setState("idle"), 8000);
        return;
      }
    }
    if (Date.now() - pollStarted > CHECK_TIMEOUT_MS) {
      backToComposing("The checks are taking too long. You were not charged; try again.");
    }
  }

  function startChecking(tok) {
    checkoutAsked = false;
    token = tok;
    saveToken(tok);
    pollStarted = Date.now();
    setState("checking");
    clearTimers();
    pollTimer = setInterval(checkPoll, 1000);
    checkPoll();
  }

  async function requestCheckout() {
    try {
      const r = await fetch("/api/paid-requests/" + encodeURIComponent(token) + "/checkout",
                            { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) {
        backToComposing(d.detail || "Payments are having a moment. You were not charged.");
        return;
      }
      clientSecret = d.client_secret;
      publishableKey = d.publishable_key;
      await startPaying();
    } catch (e) {
      backToComposing("Couldn't reach the station. You were not charged; try again.");
    }
  }

  function startConfirming(tok) {
    token = tok;
    saveToken(tok);
    pollStarted = Date.now();
    showMsg("Payment received — confirming…", true);
    setState("confirming");
    clearTimers();
    pollTimer = setInterval(poll, 2000);
    poll();
  }

  async function startPaying() {
    showMsg("", true);
    setState("paying");
    try {
      const stripe = await loadStripe(publishableKey);
      destroyCheckout();
      checkout = await stripe.initEmbeddedCheckout({
        fetchClientSecret: async () => clientSecret,
        onComplete: () => startConfirming(token),
      });
      checkout.mount("#pay-checkout");
    } catch (e) {
      destroyCheckout();
      showMsg("Payments couldn't load. You were not charged.", false);
      setState("composing");
    }
  }

  async function submitPaid() {
    const lyrics = taLyrics.value.trim(), music = taMusic.value.trim(),
          art = taArt.value.trim();
    if (!lyrics && !music) {
      showMsg("Give us a subject or a sound and we'll take it from there.", false);
      (lyrics || !music ? taLyrics : taMusic).focus();
      return;
    }
    showMsg("Checking with the DJ…", true);
    setState("checking");
    try {
      const r = await fetch("/api/paid-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lyrics, music, art }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 503) {
        showMsg(d.off_air
          ? "The station is off the air. Requests are closed."
          : "Paid requests are closed right now.", false);
        setState("composing");
        return;
      }
      if (r.status === 429 || r.status === 422 || r.status === 424) {
        showMsg(d.detail || "That didn't work. Try again.", false);
        setState("composing");
        return;
      }
      if (!d.ok) {
        showMsg("Head's up: " + (d.reason || "that didn't pass the DJ's taste filter."), false);
        setState("composing");
        return;
      }
      startChecking(d.token);
    } catch (e) {
      showMsg("Couldn't reach the station. Try again.", false);
      setState("composing");
    }
  }

  function setEnabled(enabled) {
    if (enabled) {
      btn.disabled = false;
      btn.textContent = LABEL;
    } else {
      btn.disabled = true;
      btn.textContent = LABEL + " (closed)";
    }
  }

  btn.addEventListener("click", () => {
    if (state !== "idle") return;
    showMsg("");
    loadDraft();
    setState("composing");
    taLyrics.focus();
  });

  for (const ta of boxes) ta.addEventListener("input", saveDraft);
  go.addEventListener("click", submitPaid);
  cancel.addEventListener("click", () => {
    if (state === "paying") destroyCheckout();
    clearTimers();
    clearToken();
    token = null;
    clientSecret = null;
    clearDraft();
    setState("idle");
  });

  window.PD_PAY = { setEnabled };

  const resumed = loadToken();
  if (resumed) {
    startChecking(resumed);   // checkPoll routes to the right phase
  } else {
    setState("idle");
  }
})();
