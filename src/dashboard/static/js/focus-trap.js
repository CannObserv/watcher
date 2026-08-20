/**
 * Shared focus trap for aria-modal dialogs (#39).
 *
 * aria-modal="true" promises assistive tech the rest of the page is inert;
 * this utility is what makes that true. Behaviors: capture the trigger
 * (document.activeElement) on activate, move focus into the dialog
 * ([data-focus-trap-initial] preferred, else first focusable — inputs are
 * pre-selected), contain Tab/Shift-Tab within the dialog, close on Escape,
 * and restore focus to the trigger on deactivate.
 *
 * Consumers:
 *  - Declarative: mark the dialog [data-focus-trap]. Dialogs HTMX swaps into
 *    the page arm automatically on htmx:afterSwap (they render visible, so
 *    swap-in is open — e.g. the API-key modal). Escape then hides the dialog,
 *    unless it opts out with data-focus-trap-escape="ignore" — required where
 *    closing is destructive (the one-time key display: Escape must never be
 *    a one-keystroke key loss).
 *  - Imperative: window.focusTrap.activate(dialog, {onEscape}) /
 *    .deactivate(dialog) for dialogs with their own open/close wiring
 *    (the mobile drawer in base.html).
 *
 * One trap is active at a time — the app has no stacked dialogs.
 */
(function () {
  "use strict";

  var FOCUSABLE =
    "a[href], button:not([disabled]), input:not([disabled]), " +
    "select:not([disabled]), textarea:not([disabled]), " +
    "[tabindex]:not([tabindex='-1'])";

  var active = null; // { dialog, onEscape, restoreTo }

  function focusables(dialog) {
    return Array.prototype.slice.call(dialog.querySelectorAll(FOCUSABLE));
  }

  function activate(dialog, opts) {
    active = {
      dialog: dialog,
      onEscape: (opts && opts.onEscape) || null,
      restoreTo: document.activeElement,
    };
    var initial = dialog.querySelector("[data-focus-trap-initial]") || focusables(dialog)[0];
    if (initial) {
      initial.focus();
      if (typeof initial.select === "function") initial.select();
    }
  }

  function deactivate(dialog) {
    if (!active || active.dialog !== dialog) return;
    var restoreTo = active.restoreTo;
    active = null;
    if (restoreTo && typeof restoreTo.focus === "function") restoreTo.focus();
  }

  document.addEventListener("keydown", function (evt) {
    if (!active) return;
    var dialog = active.dialog;
    if (!document.contains(dialog)) {
      active = null; // dialog was swapped out from under the trap
      return;
    }

    if (evt.key === "Escape") {
      if (dialog.getAttribute("data-focus-trap-escape") === "ignore") return;
      evt.preventDefault();
      var onEscape = active.onEscape;
      if (onEscape) {
        onEscape(); // consumer hides the dialog (and may deactivate itself)
      } else {
        dialog.classList.add("hidden");
      }
      deactivate(dialog); // idempotent if onEscape already deactivated
      return;
    }

    if (evt.key !== "Tab") return;
    var items = focusables(dialog);
    if (items.length === 0) {
      evt.preventDefault();
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    var current = document.activeElement;
    if (!dialog.contains(current)) {
      evt.preventDefault();
      (evt.shiftKey ? last : first).focus();
    } else if (evt.shiftKey && current === first) {
      evt.preventDefault();
      last.focus();
    } else if (!evt.shiftKey && current === last) {
      evt.preventDefault();
      first.focus();
    }
  });

  document.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.target;
    if (!target || typeof target.querySelector !== "function") return;
    var dialog =
      target.hasAttribute && target.hasAttribute("data-focus-trap")
        ? target
        : target.querySelector("[data-focus-trap]");
    if (dialog && (!active || active.dialog !== dialog)) activate(dialog);
  });

  window.focusTrap = { activate: activate, deactivate: deactivate };
})();
