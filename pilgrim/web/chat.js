/* Pilgrim Dot Farm — THE SHEEP PEN (SHEEP_PEN.md §2, §9.1).
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

  const token = (window.PD_VOTER && window.PD_VOTER.id())
    ? window.PD_VOTER.id() : "";
  let name = null;
  let ws = null;
  let lastId = "";
  let seen = new Set();
  let reconnectTimer = null;
  let backoff = RECONNECT_MIN;
  let slowInterval = 0;
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

  function updateCompose() {
    const ready = !!name && ws && ws.readyState === WebSocket.OPEN;
    input.disabled = !ready;
    sendBtn.disabled = !ready;
    input.placeholder = name
      ? "Say something to the pen…"
      : "Read the pen — pick a name to join in.";
    nameInput.value = name || "";
    nameBtn.textContent = name ? "Change" : "Claim";
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
      } else if (body.code === "name_changes") {
        showErr("Too many name changes today — try again tomorrow.");
      } else if (body.code === "name_length") {
        showErr("Names are 3 to 24 characters.");
      } else {
        showErr("That name can't be used.");
      }
    } catch (e) {
      showErr("Couldn't reach the pen — try again.");
    } finally {
      nameBtn.disabled = false;
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
        if (frame.banned_until) {
          systemMessage("You're banned from posting until " +
            new Date(frame.banned_until * 1000).toLocaleTimeString() + ".");
        }
        setStatus("in the pen", "on");
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
