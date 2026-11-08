/* Pilgrim Dot Farm — app shell (UI_REFACTOR.md, RADIO §9.1).
   Desktop (>= 900 px): a left nav that scrolls #desktop to a pane and
   highlights the pane in view. Phone (<= 899 px): one pane at a time, four
   bottom tabs, and a mini player that mirrors #btn-play / #indicator /
   pd:nowplaying. Nothing here touches audio: the mini PLAY button clicks the
   real #btn-play from inside its own click handler, so the AudioContext is
   still created in a user gesture (RADIO §9.2, AGENTS hard rules 1 and 9). */
(() => {
  "use strict";

  const MQ = matchMedia("(max-width: 899px)");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const desk = document.getElementById("desktop");
  const navD = document.getElementById("nav-desktop");
  if (!desk || !navD) return;
  const wins = Array.from(desk.querySelectorAll(":scope > .window"));
  const byName = {};
  wins.forEach(w => { byName[w.dataset.win] = w; });
  if (!wins.length) return;

  function mark(nav, name) {
    if (!nav) return;
    nav.querySelectorAll("[data-go]").forEach(b => {
      if (b.dataset.go === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  }

  /* ---------------- desktop: scroll + highlight ---------------- */
  function goTo(name) {
    const w = byName[name];
    if (!w) return;
    // #desktop is position:relative, so offsetTop is relative to it; 16 px is
    // its own top padding, so the pane lands flush with the first pane's y.
    desk.scrollTo({ top: Math.max(0, w.offsetTop - 16),
                    behavior: reduced.matches ? "auto" : "smooth" });
    mark(navD, name);
  }

  let raf = 0;
  function highlight() {
    raf = 0;
    if (MQ.matches) return;
    let name = wins[0].dataset.win;
    if (desk.scrollTop <= 1) {
      // A fresh page always reads RADIO, even on a very tall viewport where
      // the 35% line would otherwise land in a later pane (feedback 2026-11-08).
      name = wins[0].dataset.win;
    } else if (desk.scrollTop + desk.clientHeight >= desk.scrollHeight - 4) {
      name = wins[wins.length - 1].dataset.win;
    } else {
      const line = desk.scrollTop + desk.clientHeight * 0.35;
      for (const w of wins) if (w.offsetTop <= line) name = w.dataset.win;
    }
    mark(navD, name);
  }
  desk.addEventListener("scroll", () => {
    if (!raf) raf = requestAnimationFrame(highlight);
  }, { passive: true });
  addEventListener("resize", () => { if (!raf) raf = requestAnimationFrame(highlight); });

  /* ---------------- phone: one visible pane ---------------- */
  // STEP 3 INSERT A
  const navM = document.getElementById("nav-mobile");
  const TAB_KEY = "pd_tab";
  let active = "radio";
  try {
    const s = sessionStorage.getItem(TAB_KEY);
    if (s && byName[s]) active = s;
  } catch (e) { /* private mode */ }

  function show(name) {
    if (!byName[name]) return;
    active = name;
    wins.forEach(w => w.classList.toggle("active", w.dataset.win === name));
    mark(navM, name);
    try { sessionStorage.setItem(TAB_KEY, name); } catch (e) { /* ignore */ }
    // Entering CHAT shows the newest messages, not wherever the log was left.
    // (.chat-log has scroll-behavior:smooth; snap this jump so it is done
    // before the next paint.)
    if (name === "sheep") {
      requestAnimationFrame(() => {
        const log = document.getElementById("chat-log");
        if (!log) return;
        const prev = log.style.scrollBehavior;
        log.style.scrollBehavior = "auto";
        log.scrollTop = log.scrollHeight;
        log.style.scrollBehavior = prev;
      });
    }
  }

  /* ---------------- mode + clicks ---------------- */
  function apply() {
    if (MQ.matches) {
      // STEP 3 INSERT B
      show(active);
    } else {
      wins.forEach(w => w.classList.remove("active"));
      highlight();
    }
  }
  if (MQ.addEventListener) MQ.addEventListener("change", apply);
  else MQ.addListener(apply);

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    const b = t && t.closest ? t.closest("[data-go]") : null;
    if (!b) return;
    if (MQ.matches) { show(b.dataset.go); }
    else goTo(b.dataset.go);
  });

  // STEP 3 INSERT D (mini player)
  /* ---------------- mini player: a mirror of the radio pane ---------------- */
  const mini = document.getElementById("mini");
  const miniPlay = document.getElementById("mini-play");
  const miniText = document.getElementById("mini-text");
  const btn = document.getElementById("btn-play");
  const ind = document.getElementById("indicator");
  let song = null;   // the audible named song from pd:nowplaying, or null

  function syncMini() {
    if (!mini || !miniPlay || !miniText || !btn || !ind) return;
    const playing = btn.classList.contains("stop");
    miniPlay.textContent = playing ? "■" : "▶";
    miniPlay.classList.toggle("stop", playing);
    miniPlay.setAttribute("aria-label", playing ? "Stop" : "Play");
    miniPlay.disabled = btn.disabled;
    const st = ind.classList.contains("onair") ? "onair"
             : ind.classList.contains("buffering") ? "buffering" : "idle";
    mini.classList.remove("onair", "buffering", "idle");
    mini.classList.add(st);
    if (song) miniText.textContent = song.title + " — " + song.artist;
    else if (st === "buffering") miniText.textContent = "BUFFERING…";
    else if (st === "onair") miniText.textContent = "ON AIR";
    else miniText.textContent = btn.disabled ? "OFF THE AIR" : "PILGRIM DOT FARM";
  }
  document.addEventListener("pd:nowplaying", (ev) => {
    const d = ev.detail;
    song = (d && d.title && d.artist) ? d : null;
    syncMini();
  });
  if (btn && ind && window.MutationObserver) {
    new MutationObserver(syncMini).observe(btn, { attributes: true, attributeFilter: ["class", "disabled"] });
    new MutationObserver(syncMini).observe(ind, { attributes: true, attributeFilter: ["class"] });
  }
  if (miniPlay && btn) miniPlay.addEventListener("click", () => btn.click());
  syncMini();
  // STEP 4 INSERT E (visual viewport / keyboard)
  /* ---------------- visual viewport / keyboard (D6) ----------------
     The shell is as tall as the VISUAL viewport (iOS keeps the layout
     viewport under the keyboard; Android resizes it with
     interactive-widget=resizes-content). While a text field has focus and the
     keyboard has taken >= 120 px, body.kbd-open hides the tabs + mini player
     so the composer sits right above the keys. iOS also scrolls the window to
     reveal the field; we pin it back so the shell never drifts off-screen. */
  const vv = window.visualViewport;
  let baseH = 0, baseW = 0;
  function typing() {
    const el = document.activeElement;
    return !!el && /^(INPUT|TEXTAREA)$/.test(el.tagName);
  }
  function viewport() {
    const root = document.documentElement;
    if (!MQ.matches) {
      root.style.removeProperty("--vvh");
      document.body.classList.remove("kbd-open");
      return;
    }
    const h = vv ? vv.height : innerHeight;
    if (innerWidth !== baseW) { baseW = innerWidth; baseH = 0; }   // rotation
    if (!typing()) baseH = Math.max(baseH, h);
    root.style.setProperty("--vvh", Math.round(h) + "px");
    document.body.classList.toggle("kbd-open", typing() && baseH - h >= 120);
    if ((vv && vv.offsetTop > 0) || window.scrollY > 0) window.scrollTo(0, 0);
  }
  if (vv) {
    vv.addEventListener("resize", viewport);
    vv.addEventListener("scroll", viewport);
  }
  addEventListener("resize", viewport);
  addEventListener("orientationchange", () => setTimeout(viewport, 300));
  document.addEventListener("focusin", (ev) => {
    const el = ev.target;
    if (!MQ.matches || !el || !/^(INPUT|TEXTAREA)$/.test(el.tagName)) return;
    // after the keyboard animation: resize the shell, then bring the field
    // inside its scrolling pane body (REQUEST textareas, the name row)
    setTimeout(() => {
      viewport();
      try { el.scrollIntoView({ block: "nearest" }); } catch (e) { /* old browsers */ }
    }, 350);
  });
  document.addEventListener("focusout", () => setTimeout(viewport, 100));
  if (MQ.addEventListener) MQ.addEventListener("change", viewport);
  else MQ.addListener(viewport);
  viewport();

  apply();
})();
