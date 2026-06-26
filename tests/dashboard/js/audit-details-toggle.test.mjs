// Behavior tests for src/dashboard/static/js/audit-details-toggle.js (#216 CR-17).
//
// The dashboard has no JS test harness, so this exercises the real toggle logic
// against a minimal DOM stub using Node's built-in runner: `node --test`.
// A pytest wrapper (tests/dashboard/test_audit_details_toggle_js.py) runs it inside
// the suite so the behavior is covered in pre-ship, not just manually.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const jsPath = path.resolve(here, "../../../src/dashboard/static/js/audit-details-toggle.js");
const source = readFileSync(jsPath, "utf8");

// Minimal DOM: enough surface for the toggle handler (closest, getElementById,
// querySelector, hidden, get/setAttribute, focus). Attribute/tag selectors only.
function makeDom() {
  const elements = [];

  function matches(el, sel) {
    let m;
    if (sel === "tr") return el.tagName === "TR";
    if ((m = sel.match(/^\[([\w-]+)="(.*)"\]$/))) return el.attrs[m[1]] === m[2];
    if ((m = sel.match(/^\[([\w-]+)\]$/))) return m[1] in el.attrs;
    return false;
  }

  function makeEl(opts = {}) {
    const el = {
      tagName: (opts.tagName || "div").toUpperCase(),
      attrs: { ...(opts.attrs || {}) },
      hidden: !!opts.hidden,
      parent: null,
      focusCount: 0,
      get id() {
        return this.attrs.id;
      },
      getAttribute(n) {
        return n in this.attrs ? this.attrs[n] : null;
      },
      setAttribute(n, v) {
        this.attrs[n] = String(v);
      },
      focus() {
        this.focusCount++;
      },
      closest(sel) {
        let cur = this;
        while (cur) {
          if (matches(cur, sel)) return cur;
          cur = cur.parent;
        }
        return null;
      },
    };
    elements.push(el);
    return el;
  }

  let clickHandler = null;
  const document = {
    addEventListener(type, fn) {
      if (type === "click") clickHandler = fn;
    },
    getElementById(id) {
      return elements.find((e) => e.attrs.id === id) || null;
    },
    querySelector(sel) {
      return elements.find((e) => matches(e, sel)) || null;
    },
  };

  return { makeEl, document, click: (target) => clickHandler({ target }) };
}

function setup() {
  const dom = makeDom();
  const rowId = "audit-payload-01TEST";
  const row = dom.makeEl({ tagName: "tr", attrs: { id: rowId }, hidden: true });
  const viewBtn = dom.makeEl({
    attrs: { "data-audit-view": "", "aria-expanded": "false", "aria-controls": rowId },
  });
  const closeBtn = dom.makeEl({ attrs: { "data-audit-close": "" } });
  closeBtn.parent = row;
  vm.runInNewContext(source, { document: dom.document }); // registers the click listener
  return { dom, row, viewBtn, closeBtn };
}

test("View opens the payload row and sets aria-expanded=true", () => {
  const { dom, row, viewBtn } = setup();
  dom.click(viewBtn);
  assert.equal(row.hidden, false);
  assert.equal(viewBtn.getAttribute("aria-expanded"), "true");
});

test("View toggles the row closed on a second click", () => {
  const { dom, row, viewBtn } = setup();
  dom.click(viewBtn);
  dom.click(viewBtn);
  assert.equal(row.hidden, true);
  assert.equal(viewBtn.getAttribute("aria-expanded"), "false");
});

test("Close collapses the row and returns focus to the View button", () => {
  const { dom, row, viewBtn, closeBtn } = setup();
  dom.click(viewBtn);
  dom.click(closeBtn);
  assert.equal(row.hidden, true);
  assert.equal(viewBtn.getAttribute("aria-expanded"), "false");
  assert.equal(viewBtn.focusCount, 1);
});
