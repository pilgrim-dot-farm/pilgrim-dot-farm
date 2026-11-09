/* Pilgrim Dot Farm — PAID REQUE$T (docs/shipped/payments.md §8.3, Step 12b;
   docs/shipped/prompt-upgrade.md Step 5, RADIO §9.1).
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
  const panel = $("pay-panel");
  const taLyrics = $("pay-lyrics"), taMusic = $("pay-music"), taArt = $("pay-art");
  const advanced = panel ? panel.querySelector(".pay-advanced") : null;
  const boxes = [taLyrics, taMusic, taArt].filter(Boolean);
  const go = $("pay-go"), msg = $("pay-msg");
  const mount = $("pay-checkout");
  const stepsEl = $("pay-steps");
  const promoInput = $("pay-promo"), promoApply = $("pay-promo-apply"),
        promoMsg = $("pay-promo-msg");
  if (!panel || !mount || boxes.length !== 3) return;

  const STORE_KEY = "pd_pay_token";
  const STORE_TTL_MS = 10 * 60 * 1000;
  const DRAFT_KEY = "pd_pay_draft";
  const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

  let state = "idle";
  let paidEnabled = true;
  let checkout = null;
  let clientSecret = null;
  let publishableKey = null;
  let token = null;
  let pollTimer = null;
  let doneTimer = null;
  let pollStarted = 0;
  let stripePromise = null;
  let promoCode = null;      // validated operator comp code (never persisted)
  let promoAttempts = 2;     // feedback 2026-11-08: two wrong tries, then grey

  function showMsg(text, ok) {
    msg.textContent = text || "";
    msg.classList.toggle("hidden", !text);
    msg.style.color = ok ? "var(--ok)" : "var(--warn)";
  }

  function setState(next) {
    state = next;
    render();
  }

  /* Enumerated wait states (feedback 2026-11-08): the server's real stages. */
  const STEP_ORDER = ["moderation", "copyright", "checkout"];
  function showSteps(stage) {
    if (!stepsEl) return;
    stepsEl.hidden = false;
    const at = STEP_ORDER.indexOf(stage);
    Array.from(stepsEl.children).forEach((li, i) => {
      li.classList.toggle("active", i === at);
      li.classList.toggle("done", at > i);
    });
  }
  function hideSteps() {
    if (!stepsEl) return;
    stepsEl.hidden = true;
    Array.from(stepsEl.children).forEach((li) => {
      li.classList.remove("active", "done");
    });
  }

  function setPromoMsg(text, ok) {
    if (!promoMsg) return;
    promoMsg.textContent = text || "";
    promoMsg.classList.toggle("ok", !!text && ok);
    promoMsg.classList.toggle("err", !!text && !ok);
  }

  /* APPLY validates the code server-side (constant time, never echoed). The
     client counts two failures and then greys the row out; a network failure
     does not spend an attempt. */
  async function applyPromo() {
    if (!promoInput || !promoApply || promoCode || promoAttempts <= 0) return;
    const code = promoInput.value.trim();
    if (!code) { setPromoMsg("Enter a promo code first.", false); return; }
    promoApply.disabled = true;
    try {
      const r = await fetch("/api/paid-requests/promo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        promoCode = code;
        promoInput.disabled = true;
        setPromoMsg("Success! This one's on us", true);
        return;
      }
      if (!r.ok) throw new Error("promo check failed");
      promoAttempts -= 1;
      if (promoAttempts > 0) {
        promoApply.disabled = false;
        setPromoMsg("FAILED: " + promoAttempts + " attempts remain.", false);
      } else {
        promoInput.disabled = true;
        setPromoMsg("FAILED: no attempts remain.", false);
      }
    } catch (e) {
      promoApply.disabled = false;
      setPromoMsg("Couldn't check the code. Try again.", false);
    }
  }

  function resetPromo() {
    promoCode = null;
    promoAttempts = 2;
    if (promoInput) { promoInput.value = ""; promoInput.disabled = false; }
    if (promoApply) promoApply.disabled = false;
    setPromoMsg("");
  }

  function render() {
    const locked = state === "checking" || state === "paying" || state === "confirming";
    for (const ta of boxes) ta.disabled = locked;
    if (advanced) advanced.open = false;
    setEnabled(paidEnabled);
    // the promo box is frozen while the request is in flight, and stays grey
    // after two wrong tries or a successful code
    if (promoInput && !promoCode) promoInput.disabled = locked || promoAttempts <= 0;
    if (promoApply && !promoCode) promoApply.disabled = locked || promoAttempts <= 0;
    if (state !== "checking") hideSteps();
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
        resetPromo();
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
          showSteps("checkout");
          await requestCheckout();
          return;
        }
        if (STEP_ORDER.includes(d.stage)) showSteps(d.stage);
      } else if (d.status === "rejected") {
        backToComposing("Head's up: " + (d.reason || "that didn't pass the DJ's taste filter."));
        return;
      } else if (d.status === "abandoned" || d.status === "failed") {
        backToComposing("Couldn't finish checking your request. You were not charged; try again.");
        return;
      } else if (d.status === "awaiting_payment") {
        // Resumed after a checkout that was never completed (or a payment
        // whose webhook has not landed). Do not lock the form: clear the saved
        // token and let the listener compose again. If they did pay, the
        // server-side webhook still puts that request in the line.
        backToComposing("Your last checkout wasn't finished. If you paid, your request is still in the line.");
        return;
      } else if (["queued", "producing", "ready", "aired"].includes(d.status)) {
        clearTimers();
        clearToken();
        const pos = d.position ? " (#" + d.position + ")" : "";
        showMsg("✓ You're in the priority line" + pos + ".", true);
        clearDraft();
        resetPromo();
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
    showSteps("checkout");
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
    // Feedback 2026-11-08: both departments one and two are required.
    if (!lyrics || !music) {
      showMsg("Both LYRICS and MUSICAL DIRECTION are required.", false);
      (!lyrics ? taLyrics : taMusic).focus();
      return;
    }
    if (promoInput && promoInput.value.trim() && !promoCode) {
      showMsg("Apply your promo code first.", false);
      promoInput.focus();
      return;
    }
    showMsg("Sending your request…", true);
    setState("checking");
    showSteps("moderation");
    if (stepsEl) stepsEl.scrollIntoView({ block: "nearest" });
    try {
      const body = { lyrics, music, art };
      if (promoCode) body.promo = promoCode;
      const r = await fetch("/api/paid-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
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
    paidEnabled = enabled;
    go.disabled = !enabled || ["checking", "paying", "confirming"].includes(state);
    go.textContent = enabled ? "CHECK IT & PAY" : "PRIORITY REQUESTS CLOSED";
  }

  for (const ta of boxes) ta.addEventListener("input", saveDraft);
  go.addEventListener("click", submitPaid);
  if (promoApply) promoApply.addEventListener("click", applyPromo);
  if (promoInput) promoInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); applyPromo(); }
  });

  window.PD_PAY = { setEnabled };

  loadDraft();
  const resumed = loadToken();
  if (resumed) {
    startChecking(resumed);   // checkPoll routes to the right phase
  } else {
    setState("idle");
  }
})();
