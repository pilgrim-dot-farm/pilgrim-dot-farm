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
    if (desk.scrollTop + desk.clientHeight >= desk.scrollHeight - 4) {
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

  /* ---------------- mode + clicks ---------------- */
  function apply() {
    if (MQ.matches) {
      // STEP 3 INSERT B
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
    if (MQ.matches) { /* STEP 3 INSERT C */ }
    else goTo(b.dataset.go);
  });

  // STEP 3 INSERT D (mini player)
  // STEP 4 INSERT E (visual viewport / keyboard)

  apply();
})();
