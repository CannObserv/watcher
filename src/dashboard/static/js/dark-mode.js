/* Watcher dashboard — color-scheme toggle (three-state: light / system / dark).
 * Reads/writes localStorage key 'watcher-color-scheme'.
 *   'light'  → force light (no .dark class; absence of .dark renders light)
 *   'dark'   → force dark  (html.dark)
 *   absent   → follow OS prefers-color-scheme (system)
 * Clicking a theme-toggle button cycles the *stored* preference:
 *   light → system → dark → light.
 * The "system" state is the absent key (localStorage.removeItem), so the FOUC
 * <head> script in base.html needs no extra case — its s===null branch already
 * resolves the OS preference at first paint.
 *
 * Watcher's Tailwind dark variant is purely class-based
 * (@custom-variant dark (&:where(.dark, .dark *))) with no @media path, so —
 * unlike a media-query theme — "system" can NOT be expressed as "no class, let
 * CSS follow the OS". This script resolves the system state to the .dark class
 * via matchMedia at apply time, and re-resolves on OS theme changes while
 * system is active.
 *
 * The cycle is driven off the *stored* preference, not the rendered class:
 * 'system' (when the OS is light) and explicit 'light' both render classless and
 * are indistinguishable by class alone.
 *
 * META is the single source of truth for each state's icon + aria-label. The
 * server can't know the client's stored preference, so base.html renders a
 * neutral default and this script populates the live state on load, syncing both
 * the desktop (#theme-toggle) and mobile (#theme-toggle-mobile) buttons.
 *
 * Uses document-level click delegation and re-syncs the buttons on
 * htmx:afterSettle. The toggle buttons live in base.html's persistent chrome
 * (sidebar + mobile topbar), so they are never replaced by an HTMX partial swap
 * (watcher uses no hx-boost); delegation is defensive — it survives any future
 * swap/boost and keeps the handler registered exactly once. Dispatches
 * 'watcher:theme-changed' whenever the rendered scheme flips so diff-viewer.js
 * can re-render.
 */
(function () {
  var KEY = "watcher-color-scheme";
  var SELECTOR = "#theme-toggle, #theme-toggle-mobile";
  var mq = window.matchMedia("(prefers-color-scheme: dark)");

  /* Successor of each state in the cycle ring. */
  var NEXT = { light: "system", system: "dark", dark: "light" };

  /* Single source of truth for the button affordance per state — current-state
   * convention: the icon shows the active state; the label names it and the
   * next action in the cycle. ☀ light · ◑ system · ☽ dark. */
  var META = {
    light: { icon: "☀", label: "Color theme: Light. Activate for System." },
    system: { icon: "◑", label: "Color theme: System. Activate for Dark." },
    dark: { icon: "☽", label: "Color theme: Dark. Activate for Light." },
  };

  /* Session fallback for environments where localStorage throws (private mode /
   * disabled storage). Once any read or write throws we latch `storageBroken`
   * and drive the cycle off `memState` — covers the asymmetric case where the
   * read succeeds but the write fails, which would otherwise re-read the empty
   * key every click and pin the cycle on system→dark. */
  var memState = null;
  var storageBroken = false;

  /* Stored preference → 'light' | 'dark' | 'system'. Anything else (absent,
   * legacy, junk, or unavailable storage) means follow OS. */
  function storedState() {
    if (!storageBroken) {
      try {
        var v = localStorage.getItem(KEY);
        return v === "light" || v === "dark" ? v : "system";
      } catch (e) {
        storageBroken = true;
      }
    }
    return memState || "system";
  }

  function persist(state) {
    memState = state;
    if (storageBroken) return;
    try {
      if (state === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, state);
    } catch (e) {
      storageBroken = true;
    }
  }

  /* Whether the page should render dark for a stored state: explicit states are
   * absolute; 'system' defers to the OS media query. */
  function resolveDark(state) {
    return state === "dark" || (state === "system" && mq.matches);
  }

  function applyState(state) {
    var html = document.documentElement;
    var was = html.classList.contains("dark");
    var dark = resolveDark(state);
    html.classList.toggle("dark", dark);
    persist(state);
    syncBtns();
    if (dark !== was) {
      document.dispatchEvent(
        new CustomEvent("watcher:theme-changed", { detail: { theme: dark ? "dark" : "light" } })
      );
    }
  }

  /* Sync every theme-toggle button's icon + label with the stored preference.
   * Called on load and after any HTMX swap (htmx:afterSettle). */
  function syncBtns() {
    var m = META[storedState()];
    var btns = document.querySelectorAll(SELECTOR);
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-label", m.label);
      var icon = btns[i].querySelector("[data-theme-icon]");
      if (icon) icon.textContent = m.icon;
    }
  }

  document.addEventListener("click", function (e) {
    if (!e.target.closest(SELECTOR)) return;
    applyState(NEXT[storedState()]);
  });

  /* Follow OS theme changes live while in the system state. Guarded: legacy
   * engines (Safari < 14) exposed only MediaQueryList.addListener — skip the
   * live-follow rather than throw and suppress the initial syncBtns() below. */
  if (mq.addEventListener) {
    mq.addEventListener("change", function () {
      if (storedState() === "system") applyState("system");
    });
  }

  document.addEventListener("htmx:afterSettle", syncBtns);

  syncBtns();
})();
