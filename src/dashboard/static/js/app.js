/**
 * watcher dashboard — custom JS
 */
(function () {
  var DISMISS_MS = 5000;

  function setupAutoDismiss(el) {
    var timer;
    function start() {
      timer = setTimeout(function () { el.remove(); }, DISMISS_MS);
    }
    function pause() { clearTimeout(timer); }
    el.addEventListener("mouseenter", pause);
    el.addEventListener("mouseleave", start);
    start();
  }

  /* Initial page load */
  document.querySelectorAll("[data-auto-dismiss]").forEach(setupAutoDismiss);

  /* HTMX-injected flash messages (OOB swaps into #flash-region) */
  document.addEventListener("htmx:afterSettle", function (evt) {
    var target = evt.detail.target;
    if (target && target.id === "flash-region") {
      target.querySelectorAll("[data-auto-dismiss]").forEach(setupAutoDismiss);
    }
  });
})();
