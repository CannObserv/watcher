// Behavior tests for src/dashboard/static/js/dark-mode.js — three-state
// color-scheme toggle (light / system / dark). Mirrors power-map#25, adapted to
// watcher: Tailwind's dark variant is purely class-based (no @media path), so
// the "system" state must resolve to the .dark class via matchMedia at apply
// time (and re-resolve on OS changes) rather than relying on CSS to follow OS.
//
// The dashboard has no JS test harness, so this exercises the real IIFE against
// a minimal DOM/localStorage/matchMedia stub using Node's built-in runner:
// `node --test`. A pytest wrapper (tests/dashboard/test_dark_mode_js.py) runs it
// inside the suite so the behavior is covered pre-ship, not just manually.
//
// Pattern: build stub → vm.runInNewContext(source) → simulate events → assert.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const jsPath = path.resolve(here, "../../../src/dashboard/static/js/dark-mode.js");
const source = readFileSync(jsPath, "utf8");

const KEY = "watcher-color-scheme";
// Current-state affordance: the icon shows the active state; the label names it
// and the next action in the cycle. (Mirrors META in dark-mode.js.)
const ICON = { light: "☀", system: "◑", dark: "☽" };
const LABEL = {
  light: "Color theme: Light. Activate for System.",
  system: "Color theme: System. Activate for Dark.",
  dark: "Color theme: Dark. Activate for Light.",
};

// Comma-separated selector matcher: supports `#id` and `[attr]` tokens only.
function matchesSel(el, sel) {
  return sel.split(",").some((raw) => {
    const part = raw.trim();
    if (part[0] === "#") return el.attrs.id === part.slice(1);
    const m = part.match(/^\[([\w-]+)\]$/);
    return m ? m[1] in el.attrs : false;
  });
}

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle(c, force) {
      if (force === undefined) {
        if (set.has(c)) { set.delete(c); return false; }
        set.add(c); return true;
      }
      if (force) set.add(c); else set.delete(c);
      return force;
    },
  };
}

// Build the whole stub world. `stored` seeds localStorage; `prefersDark` seeds
// the OS media query. The FOUC <head> script is mimicked so the initial .dark
// class matches what a real page load would render before dark-mode.js runs.
function makeWorld({ stored, prefersDark = false, mqListener = true } = {}) {
  const all = [];
  const listeners = {};
  const themeEvents = [];

  function makeEl(opts = {}) {
    const el = {
      tagName: (opts.tag || "div").toUpperCase(),
      attrs: { ...(opts.attrs || {}) },
      children: [],
      parent: null,
      _text: "",
      get id() { return this.attrs.id; },
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      setAttribute(n, v) { this.attrs[n] = String(v); },
      set textContent(v) { this._text = String(v); },
      get textContent() { return this._text; },
      closest(sel) {
        let cur = this;
        while (cur) { if (matchesSel(cur, sel)) return cur; cur = cur.parent; }
        return null;
      },
      querySelector(sel) {
        const stack = [...this.children];
        while (stack.length) {
          const node = stack.shift();
          if (matchesSel(node, sel)) return node;
          stack.push(...node.children);
        }
        return null;
      },
    };
    all.push(el);
    return el;
  }

  function button(id) {
    const btn = makeEl({ tag: "button", attrs: { id, "aria-label": "Color theme" } });
    const icon = makeEl({ tag: "span", attrs: { "data-theme-icon": "" } });
    icon.parent = btn;
    btn.children.push(icon);
    return btn;
  }

  const desktop = button("theme-toggle");
  const mobile = button("theme-toggle-mobile");

  const html = { classList: makeClassList() };

  const store = {};
  if (stored !== undefined) store[KEY] = stored;
  const localStorage = {
    throwGet: false,
    throwSet: false,
    getItem(k) { if (this.throwGet) throw new Error("getItem"); return k in store ? store[k] : null; },
    setItem(k, v) { if (this.throwSet) throw new Error("setItem"); store[k] = String(v); },
    removeItem(k) { if (this.throwSet) throw new Error("removeItem"); delete store[k]; },
  };

  // Live media-query object; the script captures it once via window.matchMedia.
  // `mqListener: false` omits addEventListener to model legacy engines
  // (Safari < 14 had only addListener) — the script must guard, not throw.
  const mq = { matches: prefersDark, _change: null };
  if (mqListener) mq.addEventListener = function (type, fn) { if (type === "change") this._change = fn; };
  const window = { matchMedia: () => mq };

  class CustomEvent {
    constructor(type, init) { this.type = type; this.detail = init && init.detail; }
  }

  const document = {
    documentElement: html,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() {},
    getElementById(id) { return all.find((e) => e.attrs.id === id) || null; },
    querySelector(sel) { return all.find((e) => matchesSel(e, sel)) || null; },
    querySelectorAll(sel) { return all.filter((e) => matchesSel(e, sel)); },
    dispatchEvent(ev) {
      if (ev.type === "watcher:theme-changed") themeEvents.push(ev);
      (listeners[ev.type] || []).forEach((fn) => fn(ev));
      return true;
    },
  };

  // Mimic the FOUC <head> script: .dark if forced dark, or system+OS-dark.
  if (stored === "dark" || (stored === undefined && prefersDark)) html.classList.add("dark");

  vm.runInNewContext(source, { document, window, localStorage, CustomEvent });

  return {
    html, store, localStorage, mq, desktop, mobile, themeEvents,
    isDark: () => html.classList.contains("dark"),
    click: (target) => (listeners.click || []).forEach((fn) => fn({ target })),
    afterSettle: () => (listeners["htmx:afterSettle"] || []).forEach((fn) => fn({})),
    setOsDark(v) { mq.matches = v; if (mq._change) mq._change({}); },
    iconOf: (btn) => btn.querySelector("[data-theme-icon]").textContent,
    labelOf: (btn) => btn.getAttribute("aria-label"),
  };
}

// --- Three-state cycle: light → system → dark → light (stored progression) ---

test("system → dark on first click (default / new user)", () => {
  const w = makeWorld();
  w.click(w.desktop);
  assert.equal(w.store[KEY], "dark");
  assert.equal(w.isDark(), true);
});

test("dark → light on click", () => {
  const w = makeWorld({ stored: "dark" });
  w.click(w.desktop);
  assert.equal(w.store[KEY], "light");
  assert.equal(w.isDark(), false);
});

test("light → system clears the key", () => {
  const w = makeWorld({ stored: "light" });
  w.click(w.desktop);
  assert.equal(KEY in w.store, false);
});

test("cycles the full ring system → dark → light → system", () => {
  const w = makeWorld();
  w.click(w.desktop);
  assert.equal(w.store[KEY], "dark");
  w.click(w.desktop);
  assert.equal(w.store[KEY], "light");
  w.click(w.desktop);
  assert.equal(KEY in w.store, false);
});

// --- System state resolves to the .dark class via the OS preference ---

test("light → system with OS dark turns the page dark", () => {
  const w = makeWorld({ stored: "light", prefersDark: true });
  w.click(w.desktop);
  assert.equal(KEY in w.store, false);
  assert.equal(w.isDark(), true);
});

test("light → system with OS light keeps the page light", () => {
  const w = makeWorld({ stored: "light", prefersDark: false });
  w.click(w.desktop);
  assert.equal(w.isDark(), false);
});

test("OS theme change is followed live while in system state", () => {
  const w = makeWorld({ prefersDark: false }); // system, OS light
  assert.equal(w.isDark(), false);
  w.setOsDark(true);
  assert.equal(w.isDark(), true);
  w.setOsDark(false);
  assert.equal(w.isDark(), false);
});

test("OS theme change is ignored when an explicit state is forced", () => {
  const w = makeWorld({ stored: "light", prefersDark: false });
  w.setOsDark(true);
  assert.equal(w.isDark(), false); // forced light unaffected by OS
});

// --- Button affordance (current-state convention; both buttons stay in sync) ---

test("shows the system affordance when no preference is stored", () => {
  const w = makeWorld();
  assert.equal(w.labelOf(w.desktop), LABEL.system);
  assert.equal(w.iconOf(w.desktop), ICON.system);
});

test("shows the dark affordance when stored dark", () => {
  const w = makeWorld({ stored: "dark" });
  assert.equal(w.labelOf(w.desktop), LABEL.dark);
  assert.equal(w.iconOf(w.desktop), ICON.dark);
});

test("shows the light affordance when stored light", () => {
  const w = makeWorld({ stored: "light" });
  assert.equal(w.labelOf(w.desktop), LABEL.light);
  assert.equal(w.iconOf(w.desktop), ICON.light);
});

test("syncs both desktop and mobile buttons", () => {
  const w = makeWorld({ stored: "dark" });
  assert.equal(w.iconOf(w.desktop), ICON.dark);
  assert.equal(w.iconOf(w.mobile), ICON.dark);
  w.click(w.mobile); // mobile click drives the same cycle → light
  assert.equal(w.store[KEY], "light");
  assert.equal(w.iconOf(w.desktop), ICON.light);
  assert.equal(w.iconOf(w.mobile), ICON.light);
});

test("treats an unknown stored value as system", () => {
  const w = makeWorld({ stored: "garbage" });
  assert.equal(w.labelOf(w.desktop), LABEL.system);
  w.click(w.desktop); // system → dark
  assert.equal(w.store[KEY], "dark");
});

// --- diff-viewer.js contract: theme-changed fires when the scheme flips ---

test("dispatches watcher:theme-changed when the rendered scheme changes", () => {
  const w = makeWorld(); // system, OS light → currently light
  w.click(w.desktop); // → dark
  assert.equal(w.themeEvents.length, 1);
  assert.equal(w.themeEvents[0].detail.theme, "dark");
});

test("does not dispatch theme-changed when the rendered scheme is unchanged", () => {
  const w = makeWorld({ stored: "light", prefersDark: false });
  w.click(w.desktop); // light → system, both render light
  assert.equal(w.themeEvents.length, 0);
});

// --- Degraded storage: cycle keeps advancing off the in-memory fallback ---

test("keeps cycling when setItem throws (write-broken)", () => {
  const w = makeWorld(); // system; reads work at load
  w.localStorage.throwSet = true;
  w.click(w.desktop); // system → dark
  assert.equal(w.isDark(), true);
  w.click(w.desktop); // dark → light (must NOT re-read empty key and stick)
  assert.equal(w.isDark(), false);
  w.click(w.desktop); // light → system
  assert.equal(w.isDark(), false);
});

test("keeps cycling when getItem throws (read-broken)", () => {
  const w = makeWorld();
  w.localStorage.throwGet = true;
  w.afterSettle(); // forces a syncBtns read through the throwing getItem
  assert.equal(w.labelOf(w.desktop), LABEL.system);
  w.click(w.desktop); // system → dark
  assert.equal(w.isDark(), true);
  w.click(w.desktop); // dark → light
  assert.equal(w.isDark(), false);
});

test("does not throw when matchMedia lacks addEventListener (legacy engine)", () => {
  // Without the guard, registering the change listener throws and suppresses the
  // initial syncBtns(), leaving the button on its neutral default. The cycle
  // must still load (icon synced) and advance on click.
  const w = makeWorld({ stored: "dark", mqListener: false });
  assert.equal(w.iconOf(w.desktop), ICON.dark); // initial sync still ran
  w.click(w.desktop); // dark → light
  assert.equal(w.store[KEY], "light");
  assert.equal(w.isDark(), false);
});

// --- HTMX boost survival: document delegation + afterSettle resync ---

test("re-syncs the button affordance on htmx:afterSettle", () => {
  const w = makeWorld({ stored: "dark" });
  w.desktop.querySelector("[data-theme-icon]").textContent = ""; // stale fresh button
  w.afterSettle();
  assert.equal(w.iconOf(w.desktop), ICON.dark);
  assert.equal(w.labelOf(w.desktop), LABEL.dark);
});
