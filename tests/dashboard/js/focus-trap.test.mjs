// Behavior tests for src/dashboard/static/js/focus-trap.js (#39).
//
// Shared focus trap for aria-modal dialogs: capture the trigger element on
// activate, move focus into the dialog, contain Tab/Shift-Tab, close on Escape
// (unless the dialog opts out), restore focus on deactivate, and auto-arm
// dialogs HTMX swaps into the page (htmx:afterSwap, not DOMContentLoaded).
//
// The dashboard has no JS test harness, so this exercises the real IIFE against
// a minimal DOM stub using Node's built-in runner: `node --test`. A pytest
// wrapper (tests/dashboard/test_focus_trap_js.py) runs it inside the suite.
//
// Pattern: build stub → vm.runInNewContext(source) → simulate events → assert.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const jsPath = path.resolve(here, "../../../src/dashboard/static/js/focus-trap.js");
const source = readFileSync(jsPath, "utf8");

// Selector matcher for the focusable-elements query and the data-attribute
// hooks. Supports exactly the selector grammar focus-trap.js uses.
function matchesPart(el, part) {
  part = part.trim();
  let m;
  if ((m = part.match(/^\[([\w-]+)\]$/))) return m[1] in el.attrs;
  if (part === "a[href]") return el.tagName === "A" && "href" in el.attrs;
  if ((m = part.match(/^(\w+):not\(\[disabled\]\)$/)))
    return el.tagName === m[1].toUpperCase() && !("disabled" in el.attrs);
  if (/^\[tabindex\]:not\(\[tabindex=.?-1.?\]\)$/.test(part))
    return "tabindex" in el.attrs && el.attrs.tabindex !== "-1";
  return false;
}

function matchesSel(el, sel) {
  return sel.split(",").some((part) => matchesPart(el, part));
}

function makeWorld() {
  const listeners = {};
  const body = { tagName: "BODY", attrs: {}, children: [], parent: null };

  function makeEl(tagName, attrs = {}, parent = null) {
    const el = {
      tagName: tagName.toUpperCase(),
      attrs: { ...attrs },
      children: [],
      parent,
      detached: false,
      focusCount: 0,
      selectCount: 0,
      classList: {
        classes: new Set((attrs.class || "").split(" ").filter(Boolean)),
        add(c) { this.classes.add(c); },
        remove(c) { this.classes.delete(c); },
        contains(c) { return this.classes.has(c); },
      },
      focus() {
        this.focusCount++;
        document.activeElement = this;
      },
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      hasAttribute(n) { return n in this.attrs; },
      setAttribute(n, v) { this.attrs[n] = String(v); },
      descendants() {
        const out = [];
        const walk = (node) => {
          for (const child of node.children) { out.push(child); walk(child); }
        };
        walk(this);
        return out;
      },
      querySelector(sel) {
        return this.descendants().find((e) => matchesSel(e, sel)) || null;
      },
      querySelectorAll(sel) {
        return this.descendants().filter((e) => matchesSel(e, sel));
      },
      contains(other) {
        let cur = other;
        while (cur) { if (cur === this) return true; cur = cur.parent; }
        return false;
      },
    };
    if (parent) parent.children.push(el);
    return el;
  }

  // Inputs get a select() so the trap can pre-select the key value.
  function makeInput(attrs, parent) {
    const el = makeEl("input", attrs, parent);
    el.select = function () { this.selectCount++; };
    return el;
  }

  const document = {
    activeElement: body,
    body,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    contains(el) { return el === body || !el.detached; },
  };

  function dispatch(type, evt) {
    for (const fn of listeners[type] || []) fn(evt);
  }

  function keydown(key, { shiftKey = false } = {}) {
    const evt = { key, shiftKey, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } };
    dispatch("keydown", evt);
    return evt;
  }

  function afterSwap(target) {
    dispatch("htmx:afterSwap", { target });
  }

  const window = {};
  vm.runInNewContext(source, { document, window });

  return { makeEl, makeInput, document, window, keydown, afterSwap, body };
}

// A dialog shaped like the API-key modal: input + two buttons.
function makeModal(world, dialogAttrs = {}) {
  const dialog = world.makeEl("div", { "data-focus-trap": "", ...dialogAttrs }, world.body);
  const inner = world.makeEl("div", {}, dialog);
  const input = world.makeInput({ id: "new-api-key-value", readonly: "" }, inner);
  const copyBtn = world.makeEl("button", {}, inner);
  const doneBtn = world.makeEl("button", {}, inner);
  return { dialog, input, copyBtn, doneBtn };
}

test("activate moves focus to the first focusable element", () => {
  const world = makeWorld();
  const { dialog, input } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  assert.equal(world.document.activeElement, input);
});

test("activate prefers [data-focus-trap-initial] and pre-selects it", () => {
  const world = makeWorld();
  const { dialog, input } = makeModal(world);
  input.attrs["data-focus-trap-initial"] = "";
  world.window.focusTrap.activate(dialog);
  assert.equal(world.document.activeElement, input);
  assert.equal(input.selectCount, 1);
});

test("Tab on the last focusable wraps to the first", () => {
  const world = makeWorld();
  const { dialog, input, doneBtn } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  doneBtn.focus();
  const evt = world.keydown("Tab");
  assert.equal(evt.defaultPrevented, true);
  assert.equal(world.document.activeElement, input);
});

test("Shift-Tab on the first focusable wraps to the last", () => {
  const world = makeWorld();
  const { dialog, input, doneBtn } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  input.focus();
  const evt = world.keydown("Tab", { shiftKey: true });
  assert.equal(evt.defaultPrevented, true);
  assert.equal(world.document.activeElement, doneBtn);
});

test("Tab with focus outside the dialog is pulled back inside", () => {
  const world = makeWorld();
  const { dialog, input } = makeModal(world);
  const outside = world.makeEl("button", {}, world.body);
  world.window.focusTrap.activate(dialog);
  outside.focus();
  const evt = world.keydown("Tab");
  assert.equal(evt.defaultPrevented, true);
  assert.equal(world.document.activeElement, input);
});

test("Escape invokes onEscape and restores focus to the trigger", () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const { dialog } = makeModal(world);
  let closed = 0;
  world.window.focusTrap.activate(dialog, { onEscape: () => { closed++; } });
  world.keydown("Escape");
  assert.equal(closed, 1);
  assert.equal(world.document.activeElement, trigger);
});

test("Escape without onEscape hides the dialog and restores focus", () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const { dialog } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  world.keydown("Escape");
  assert.equal(dialog.classList.contains("hidden"), true);
  assert.equal(world.document.activeElement, trigger);
});

test('data-focus-trap-escape="ignore" suppresses Escape entirely', () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const { dialog, input } = makeModal(world, { "data-focus-trap-escape": "ignore" });
  world.window.focusTrap.activate(dialog);
  world.keydown("Escape");
  assert.equal(dialog.classList.contains("hidden"), false);
  assert.equal(world.document.activeElement, input); // still trapped
  // Tab containment survives the ignored Escape.
  const evt = world.keydown("Tab", { shiftKey: true });
  assert.equal(evt.defaultPrevented, true);
});

test("deactivate restores focus to the element active before activate", () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const { dialog } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  world.window.focusTrap.deactivate(dialog);
  assert.equal(world.document.activeElement, trigger);
});

test("deactivate is idempotent and scoped to the active dialog", () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const { dialog } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  world.window.focusTrap.deactivate(dialog);
  const elsewhere = world.makeEl("button", {}, world.body);
  elsewhere.focus();
  world.window.focusTrap.deactivate(dialog); // second call must not steal focus
  assert.equal(world.document.activeElement, elsewhere);
});

test("htmx:afterSwap auto-arms a swapped-in [data-focus-trap] dialog", () => {
  const world = makeWorld();
  const trigger = world.makeEl("button", {}, world.body);
  trigger.focus();
  const container = world.makeEl("div", { id: "api-keys-modal-container" }, world.body);
  const dialog = world.makeEl("div", { "data-focus-trap": "" }, container);
  const input = world.makeInput({ "data-focus-trap-initial": "" }, dialog);
  world.afterSwap(container);
  assert.equal(world.document.activeElement, input);
  // The trap is armed: Tab is contained.
  const evt = world.keydown("Tab");
  assert.equal(evt.defaultPrevented, true);
});

test("htmx:afterSwap without a dialog in the target is a no-op", () => {
  const world = makeWorld();
  const container = world.makeEl("div", {}, world.body);
  world.makeEl("span", {}, container);
  world.afterSwap(container);
  const evt = world.keydown("Tab");
  assert.equal(evt.defaultPrevented, false);
});

test("a trap whose dialog left the DOM disarms itself", () => {
  const world = makeWorld();
  const { dialog } = makeModal(world);
  world.window.focusTrap.activate(dialog);
  dialog.detached = true; // swapped out
  const evt = world.keydown("Tab");
  assert.equal(evt.defaultPrevented, false);
});
