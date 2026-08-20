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
 * Traps stack (innermost wins). The app has no stacked dialogs today, but the
 * guard test funnels every future modal through here, so activating a second
 * trap must not strand the first one's restore target: each entry keeps its
 * own, and since an inner dialog's trigger sits inside the outer one, popping
 * naturally walks focus back out.
 */
(function () {
  "use strict";

  var FOCUSABLE =
    "a[href], button:not([disabled]), input:not([disabled]), " +
    "select:not([disabled]), textarea:not([disabled]), " +
    "[tabindex]:not([tabindex='-1'])";

  var stack = []; // { dialog, onEscape, restoreTo }, innermost last

  function focusables(dialog) {
    return Array.prototype.slice.call(dialog.querySelectorAll(FOCUSABLE));
  }

  function indexOf(dialog) {
    for (var i = 0; i < stack.length; i++) {
      if (stack[i].dialog === dialog) return i;
    }
    return -1;
  }

  function activate(dialog, opts) {
    if (indexOf(dialog) !== -1) return; // already armed — keep the original restoreTo
    stack.push({
      dialog: dialog,
      onEscape: (opts && opts.onEscape) || null,
      restoreTo: document.activeElement,
    });
    var hooked = dialog.querySelector("[data-focus-trap-initial]");
    var initial = hooked || focusables(dialog)[0];
    if (!initial) return;
    initial.focus();
    // Pre-select only where the dialog asked for it: auto-selecting a
    // pre-filled input means the next keystroke wipes it.
    if (hooked && typeof initial.select === "function") initial.select();
  }

  function deactivate(dialog) {
    var i = indexOf(dialog);
    if (i === -1) return;
    var entry = stack[i];
    stack.splice(i, 1);
    // Only the innermost trap owns focus; unwinding one beneath it must not
    // yank focus out of the dialog the user is actually in.
    if (i !== stack.length) return;
    if (entry.restoreTo && typeof entry.restoreTo.focus === "function") entry.restoreTo.focus();
  }

  document.addEventListener("keydown", function (evt) {
    // Dialogs swapped out from under their trap disarm themselves.
    while (stack.length && !document.contains(stack[stack.length - 1].dialog)) stack.pop();
    if (!stack.length) return;
    var entry = stack[stack.length - 1];
    var dialog = entry.dialog;

    if (evt.key === "Escape") {
      if (dialog.getAttribute("data-focus-trap-escape") === "ignore") return;
      evt.preventDefault();
      var onEscape = entry.onEscape;
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
    if (dialog) activate(dialog); // no-op when this dialog is already armed
  });

  window.focusTrap = { activate: activate, deactivate: deactivate };
})();
