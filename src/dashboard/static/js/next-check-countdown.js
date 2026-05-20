/**
 * Countdown widget for [data-next-check] elements.
 *
 * Reads an ISO 8601 UTC timestamp from the attribute and replaces the
 * element's text with a human-readable countdown or "overdue" label.
 * Refreshes every 60 seconds while the page is open.
 */
(function () {
  "use strict";

  function formatCountdown(isoTimestamp) {
    const target = new Date(isoTimestamp);
    const diffMs = target - Date.now();
    if (diffMs <= 0) return "overdue";
    const totalSec = Math.floor(diffMs / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function updateAll() {
    document.querySelectorAll("[data-next-check]").forEach(function (el) {
      el.textContent = formatCountdown(el.dataset.nextCheck);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    updateAll();
    setInterval(updateAll, 60_000);
  });

  // Re-run after HTMX swaps in case the table is dynamically updated.
  document.addEventListener("htmx:afterSwap", updateAll);
})();
