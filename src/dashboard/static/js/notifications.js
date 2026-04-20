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

  /* Public API */
  window.insertVar = insertVar;
  window.toggleVarDrawer = toggleVarDrawer;
})();
