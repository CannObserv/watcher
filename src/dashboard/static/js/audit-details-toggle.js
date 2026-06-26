/**
 * Inline payload viewer for audit-event tables (#216).
 *
 * Each Details "View" button ([data-audit-view], aria-controls=<row id>) toggles
 * a hidden read-only payload <tr>; that row's "Close" button ([data-audit-close])
 * collapses it and returns focus to the View button. Delegated on document so it
 * keeps working after HTMX swaps the audit / Recent Activity table.
 */
(function () {
  "use strict";

  function setOpen(viewBtn, row, open) {
    row.hidden = !open;
    viewBtn.setAttribute("aria-expanded", String(open));
  }

  document.addEventListener("click", function (evt) {
    const viewBtn = evt.target.closest("[data-audit-view]");
    if (viewBtn) {
      const row = document.getElementById(viewBtn.getAttribute("aria-controls"));
      if (row) setOpen(viewBtn, row, row.hidden); // hidden -> open, shown -> close
      return;
    }

    const closeBtn = evt.target.closest("[data-audit-close]");
    if (closeBtn) {
      const row = closeBtn.closest("tr");
      if (!row) return;
      const controllingBtn = document.querySelector('[aria-controls="' + row.id + '"]');
      if (controllingBtn) {
        setOpen(controllingBtn, row, false);
        controllingBtn.focus();
      } else {
        row.hidden = true;
      }
    }
  });
})();
