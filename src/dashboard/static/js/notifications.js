/**
 * Notification form — variable chips, override management, edit-template
 * toggle helpers.
 */
(function () {
  /**
   * Insert `{{ varName }}` at the current cursor position of the textarea
   * with id=targetId, or append if textarea is not focused.
   */
  function insertVar(targetId, varName) {
    var el = document.getElementById(targetId);
    if (!el) return;
    var insertion = "{{ " + varName + " }}";
    var start = el.selectionStart;
    var end = el.selectionEnd;
    var before = el.value.slice(0, start);
    var after = el.value.slice(end);
    el.value = before + insertion + after;
    el.focus();
    var pos = start + insertion.length;
    el.setSelectionRange(pos, pos);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  /**
   * Toggle a variable-reference drawer identified by drawerId.
   * Updates aria-expanded on the triggering button.
   */
  function toggleVarDrawer(drawerId, trigger) {
    var drawer = document.getElementById(drawerId);
    if (!drawer) return;
    var expanded = !drawer.hidden;
    drawer.hidden = expanded;
    if (trigger) {
      trigger.setAttribute("aria-expanded", String(!expanded));
      trigger.textContent = expanded ? "See all variables" : "Hide variables";
    }
  }

  /**
   * Keep the preview event <select> in sync with the subscribed-events
   * checkboxes. Listens for change events on `input[name=events]` and
   * adds/removes <option>s in any `select[data-preview-event-select]`
   * inside the same <form>.
   *
   * When nothing is subscribed, falls back to a single `change_detected`
   * option — matches the server-render fallback in
   * notification_form_preview_card.html and the *_new.html form default.
   */
  function syncPreviewEventSelect(form) {
    var selects = form.querySelectorAll("select[data-preview-event-select]");
    if (!selects.length) return;
    // Build the canonical (value -> label) map from the subscribe
    // checkboxes themselves: each has a <label> wrapper whose text is
    // the human-readable event title.
    var checkboxes = form.querySelectorAll('input[type=checkbox][name=events]');
    if (!checkboxes.length) return;
    var allEvents = [];
    var subscribed = [];
    checkboxes.forEach(function (cb) {
      var lbl = cb.closest("label");
      var label = lbl ? lbl.textContent.trim() : cb.value;
      allEvents.push({ value: cb.value, label: label });
      if (cb.checked && !cb.disabled) subscribed.push(cb.value);
    });
    var pool;
    if (subscribed.length) {
      pool = allEvents.filter(function (e) { return subscribed.indexOf(e.value) !== -1; });
    } else {
      // Empty-set fallback: just change_detected, matching the server-side
      // template fallback and the *_new.html form default. (change_detected
      // is always in the form's checkboxes — it's a canonical enum member —
      // so no further fallback is needed.)
      pool = allEvents.filter(function (e) { return e.value === "change_detected"; });
    }
    selects.forEach(function (sel) {
      // Re-read override state from the current options before replacing them.
      // Writing data-has-override back onto rebuilt options keeps it alive
      // across subsequent toggles without a full server re-render.
      var hasOverride = new Set();
      sel.querySelectorAll("option[data-has-override]").forEach(function (o) {
        hasOverride.add(o.value);
      });
      // Preserve the user's current selection if still in the pool;
      // otherwise default to change_detected if present, else first.
      var prev = sel.value;
      var newOptions = pool.map(function (e) {
        var opt = document.createElement("option");
        opt.value = e.value;
        if (hasOverride.has(e.value)) {
          opt.dataset.hasOverride = "1";
          opt.textContent = e.label + " •";
        } else {
          opt.textContent = e.label;
        }
        return opt;
      });
      var inPool = pool.some(function (e) { return e.value === prev; });
      var defaultVal;
      if (inPool) {
        defaultVal = prev;
      } else if (pool.some(function (e) { return e.value === "change_detected"; })) {
        defaultVal = "change_detected";
      } else if (pool.length) {
        defaultVal = pool[0].value;
      }
      newOptions.forEach(function (opt) {
        if (opt.value === defaultVal) opt.selected = true;
      });
      sel.replaceChildren.apply(sel, newOptions);
      // Fire change so HTMX preview pane refreshes with the new event.
      if (prev !== defaultVal) {
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  }

  /**
   * Wire up subscribe-checkbox listeners on every form that contains a
   * preview event selector. Runs at DOMContentLoaded and again after
   * any HTMX swap (to cover late-rendered forms).
   */
  function wirePreviewEventSync(root) {
    (root || document).querySelectorAll("select[data-preview-event-select]").forEach(function (sel) {
      var form = sel.closest("form");
      if (!form || form.dataset.previewSyncWired === "1") return;
      form.dataset.previewSyncWired = "1";
      form.addEventListener("change", function (ev) {
        var t = ev.target;
        if (t && t.matches && t.matches('input[type=checkbox][name=events]')) {
          syncPreviewEventSelect(form);
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { wirePreviewEventSync(document); });
  } else {
    wirePreviewEventSync(document);
  }
  document.body && document.body.addEventListener("htmx:afterSwap", function (ev) {
    wirePreviewEventSync(ev.target || document);
  });

  /* Public API */
  window.insertVar = insertVar;
  window.toggleVarDrawer = toggleVarDrawer;
  window.syncPreviewEventSelect = syncPreviewEventSelect;
})();
