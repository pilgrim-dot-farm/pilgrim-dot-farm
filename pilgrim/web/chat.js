/* Pilgrim Dot Farm — THE SHEEP PEN (docs/plans/sheep-pen-brief.md §2, §9.1).
   One shared chat room over a WebSocket. The browser token is the existing
   voter id (voter.js, localStorage `pd_voter`); the display name is chosen
   here. All user text is rendered with textContent, never as HTML. No build
   step, no framework, and nothing here touches radio playback. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const logEl = $("chat-log");
  if (!logEl) return;

  const input = $("chat-input"), sendBtn = $("chat-send"), countEl = $("chat-count");
  const nameInput = $("chat-name-input"), nameBtn = $("chat-name-btn");
  const errEl = $("chat-err"), statusEl = $("chat-status");

  const MAX = 200;
  const RECONNECT_MIN = 1000;
  const RECONNECT_MAX = 20000;
  const LAST_KEY = "pd_chat_last";

  /* Every display name gets a stable colour from the standard xterm 256-colour
     palette (0-255), not one shared cyan. Purple/violet/magenta hues are
     filtered out, as are colours too dark or too grey to read on the dark
     pane, so the set is readable and varied. The hash is over the lowercased
     name, so the same name is the same colour on every browser. */
  const XTERM_256 = (() => {
    const base = ["#000000", "#800000", "#008000", "#808000", "#000080",
      "#800080", "#008080", "#c0c0c0", "#808080", "#ff0000", "#00ff00",
      "#ffff00", "#0000ff", "#ff00ff", "#00ffff", "#ffffff"];
    const steps = [0, 95, 135, 175, 215, 255];
    const out = base.slice();
    for (const r of steps) for (const g of steps) for (const b of steps) {
      out.push("#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join(""));
    }
    for (let i = 0; i < 24; i++) {
      const v = 8 + i * 10;
      out.push("#" + [v, v, v].map((n) => n.toString(16).padStart(2, "0")).join(""));
    }
    return out;
  })();

  function rgbOf(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function hslOf(hex) {
    const [r, g, b] = rgbOf(hex).map((v) => v / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const l = (max + min) / 2;
    const d = max - min;
    if (d === 0) return { h: 0, s: 0, l };
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) * 60;
    else if (max === g) h = ((b - r) / d + 2) * 60;
    else h = ((r - g) / d + 4) * 60;
    return { h, s, l };
  }

  const CHAT_COLORS = (() => {
    const seen = new Set();
    const out = [];
    for (const hex of XTERM_256) {
      const { h, s, l } = hslOf(hex);
      if (l < 0.45 || s < 0.25) continue;          // readable, not grey
      if (h >= 255 && h <= 345) continue;          // no purple/violet/magenta
      if (seen.has(hex)) continue;
      seen.add(hex);
      out.push(hex);
    }
    return out.length ? out : ["#24e0ff"];
  })();

  function nameColor(name) {
    let h = 2166136261;
    const text = String(name || "").toLowerCase();
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return CHAT_COLORS[Math.abs(h) % CHAT_COLORS.length];
  }

  const token = (window.PD_VOTER && window.PD_VOTER.id())
    ? window.PD_VOTER.id() : "";
  let name = null;
  let ws = null;
  let lastId = "";
  let seen = new Set();
  let reconnectTimer = null;
  let backoff = RECONNECT_MIN;
  let slowInterval = 0;
  let cooldownUntil = 0;
  let cooldownTimer = null;
  let lastSendAt = 0;
  let hideTimer = null;

  try { lastId = localStorage.getItem(LAST_KEY) || ""; } catch (e) { /* private mode */ }

  function rememberLast(id) {
    lastId = id || lastId;
    try { localStorage.setItem(LAST_KEY, lastId); } catch (e) { /* ignore */ }
  }

  function showErr(msg, ok = false, sticky = false) {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    errEl.textContent = msg || "";
    errEl.classList.toggle("hidden", !msg);
    errEl.style.color = ok ? "var(--ok)" : "var(--warn)";
    if (msg && !sticky) hideTimer = setTimeout(() => {
      errEl.classList.add("hidden");
    }, 5000);
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.classList.remove("on", "off");
    if (cls) statusEl.classList.add(cls);
  }

  function compareIds(a, b) {
    const pa = String(a || "0-0").split("-").map(Number);
    const pb = String(b || "0-0").split("-").map(Number);
    if (pa[0] !== pb[0]) return pa[0] - pb[0];
    return (pa[1] || 0) - (pb[1] || 0);
  }

  function nearBottom() {
    return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 48;
  }

  function renderMessage(m) {
    if (!m || !m.id || seen.has(m.id)) return;
    seen.add(m.id);
    const stick = nearBottom();
    const row = document.createElement("div");
    row.className = "chat-msg" + (m.giles ? " giles" : "");
    row.dataset.id = m.id;
    const who = document.createElement("span");
    who.className = "chat-author";
    who.textContent = m.giles ? "GILES" : (m.name || "someone");
    if (!m.giles) who.style.color = nameColor(m.name);
    const body = document.createElement("span");
    body.className = "chat-text";
    body.textContent = m.text || "";   // textContent = no HTML injection
    row.appendChild(who);
    row.appendChild(body);
    logEl.appendChild(row);
    if (stick) logEl.scrollTop = logEl.scrollHeight;
    rememberLast(m.id);
  }

  function systemMessage(text) {
    const row = document.createElement("div");
    row.className = "chat-msg system";
    row.textContent = text;
    logEl.appendChild(row);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function removeMessage(id) {
    const row = logEl.querySelector('[data-id="' + CSS.escape(id) + '"]');
    if (row) row.remove();
    seen.add(id);
  }

  function coolingOff() {
    return cooldownUntil > Date.now() / 1000;
  }

  function applyCooldown(until) {
    cooldownUntil = Number(until) || 0;
    if (!coolingOff()) return;
    const seconds = Math.max(1, Math.round(cooldownUntil - Date.now() / 1000));
    const mins = seconds >= 90 ? Math.ceil(seconds / 60) + " minutes" : seconds + " seconds";
    systemMessage("Cooling off for " + mins + " — the pen is open when you're back.");
    updateCompose();
    if (cooldownTimer) clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(() => { cooldownTimer = null; updateCompose(); },
      seconds * 1000);
  }

  function updateCompose() {
    const cooled = coolingOff();
    const ready = !!name && ws && ws.readyState === WebSocket.OPEN && !cooled;
    input.disabled = !ready;
    sendBtn.disabled = !ready;
    input.placeholder = cooled
      ? "Cooling off — give it a moment."
      : name
        ? "Say something to the pen…"
        : "Read the pen — pick a name to join in.";
    nameInput.value = name || "";
    // A claimed name is locked (operator decision 2026-11-07): no client-side
    // rename path. Only the admin route can change it.
    nameInput.disabled = !!name;
    nameBtn.disabled = !!name;
    nameBtn.textContent = name ? "Claimed" : "Claim";
  }

  async function claimName() {
    const wanted = nameInput.value.trim();
    if (!token) { showErr("This browser can't make a chat identity — try a normal window."); return; }
    if (!wanted) { showErr("Pick a name first."); return; }
    nameBtn.disabled = true;
    try {
      const resp = await fetch("/api/chat/name", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        body: JSON.stringify({ token, name: wanted }),
      });
      const body = await resp.json().catch(() => ({}));
      if (resp.ok && body.ok) {
        name = body.name;
        showErr("You're in the pen as " + name + ".", true);
        updateCompose();
      } else if (body.code === "name_taken") {
        showErr("That name is taken — pick another.");
      } else if (body.code === "name_reserved") {
        showErr("That name belongs to the station.");
      } else if (body.code === "name_locked") {
        showErr("Your name is locked — ask the operator if it needs changing.");
      } else if (body.code === "name_length") {
        showErr("Names are 3 to 24 characters.");
      } else {
        showErr("That name can't be used.");
      }
    } catch (e) {
      showErr("Couldn't reach the pen — try again.");
    } finally {
      nameBtn.disabled = !!name;
    }
  }

  function sendMessage() {
    const text = input.value.trim();
    if (!text || !name) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showErr("Not connected — reconnecting…");
      return;
    }
    const now = Date.now();
    if (slowInterval && now - lastSendAt < slowInterval * 1000) {
      showErr("The pen is in slow mode — wait a moment.");
      return;
    }
    let id;
    try {
      id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    } catch (e) {
      id = String(Date.now()) + Math.random().toString(16).slice(2);
    }
    ws.send(JSON.stringify({ type: "send", client_msg_id: id, text }));
    lastSendAt = now;
    input.value = "";
    updateCount();
  }

  function updateCount() {
    countEl.textContent = input.value.length + "/" + MAX;
  }

  async function fillGap(after) {
    if (!after) return;
    try {
      const resp = await fetch("/api/chat/history?after=" + encodeURIComponent(after),
        { cache: "no-store" });
      if (!resp.ok) return;
      const body = await resp.json();
      (body.messages || []).forEach(renderMessage);
    } catch (e) { /* resync/reconnect will cover it */ }
  }

  function handle(frame) {
    switch (frame.type) {
      case "welcome": {
        name = frame.name || null;
        if (frame.slow_mode) { slowInterval = Number(frame.slow_mode) || 0; }
        (frame.history || []).forEach(renderMessage);
        if (frame.last_id && compareIds(frame.last_id, lastId) > 0) {
          fillGap(lastId);
          rememberLast(frame.last_id);
        }
        if (frame.cooldown_until) applyCooldown(frame.cooldown_until);
        if (frame.banned_until) {
          systemMessage("You're banned from posting until " +
            new Date(frame.banned_until * 1000).toLocaleTimeString() + ".");
        }
        setStatus("GRAZE UPON IT", "on");
        backoff = RECONNECT_MIN;
        updateCompose();
        break;
      }
      case "message": renderMessage(frame); break;
      case "ack": break;                    // the message arrives via `message`
      case "deleted": removeMessage(frame.id); break;
      case "renamed": name = frame.name || name; updateCompose(); break;
      case "warned":
        systemMessage("A moderator says: " + (frame.reason || "mind the rules."));
        break;
      case "cooldown": applyCooldown(frame.until); break;
      case "banned":
        systemMessage("You've been banned from posting until " +
          new Date((frame.until || 0) * 1000).toLocaleTimeString() + ".");
        break;
      case "slow_mode":
        slowInterval = Number(frame.interval_s) || 0;
        systemMessage("Slow mode: one message every " + slowInterval + "s.");
        break;
      case "error":
        if (frame.code === "rate") showErr("Easy there — give it a moment.");
        else if (frame.code === "repeat") showErr("You just said that.");
        else if (frame.code === "name_required") showErr("Pick a name first.");
        else if (frame.code === "posting_disabled") showErr("The pen is closed for a moment.");
        else if (frame.code === "name_locked") showErr("Your name is locked.");
        else if (frame.code === "cooldown") showErr("Cooling off — try again shortly.");
        else if (frame.code === "banned") showErr("You're banned from posting.");
        else showErr(frame.detail || "The pen hiccupped — try again.");
        break;
      case "ping":
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "pong" }));
        }
        break;
      case "pong": break;
      default: break;
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    const delay = Math.min(RECONNECT_MAX,
      Math.round(RECONNECT_MIN + Math.random() * (RECONNECT_MAX - RECONNECT_MIN)));
    backoff = Math.min(RECONNECT_MAX, backoff * 2);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, Math.max(delay, backoff / 2));
  }

  function connect() {
    if (!token) {
      setStatus("no identity", "off");
      showErr("This browser can't make a chat identity — try a normal window.", false, true);
      return;
    }
    setStatus("connecting…", "");
    let url;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      url = proto + "//" + location.host + "/api/chat/ws";
      ws = new WebSocket(url);
    } catch (e) {
      setStatus("offline", "off");
      scheduleReconnect();
      return;
    }
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({
        type: "hello", token, last_id: lastId || null, client_version: "20261107a",
      }));
    });
    ws.addEventListener("message", (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch (e) { return; }
      if (frame && typeof frame === "object") handle(frame);
    });
    ws.addEventListener("close", () => {
      setStatus("reconnecting…", "off");
      updateCompose();
      scheduleReconnect();
    });
    ws.addEventListener("error", () => {
      setStatus("offline", "off");
    });
  }

  input.addEventListener("input", updateCount);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); sendMessage(); }
  });
  sendBtn.addEventListener("click", sendMessage);
  nameBtn.addEventListener("click", claimName);
  nameInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); claimName(); }
  });

  updateCount();
  updateCompose();
  connect();
})();
