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

  function showFlash(level, message) {
    var region = document.getElementById("flash-region");
    if (!region) return;
    var el = document.createElement("div");
    el.className = "flash flash-" + level + " flex items-center justify-between mb-4";
    el.setAttribute("data-auto-dismiss", "");
    el.setAttribute("role", "alert");
    var span = document.createElement("span");
    span.textContent = message;
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "ms-4 text-current opacity-60 hover:opacity-100";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.innerHTML = '<span aria-hidden="true">&times;</span>';
    dismiss.addEventListener("click", function () { el.remove(); });
    el.appendChild(span);
    el.appendChild(dismiss);
    region.appendChild(el);
    setupAutoDismiss(el);
  }

  /* Public API */
  window.watcher = { showFlash: showFlash };

  /* Initial page load */
  document.querySelectorAll("[data-auto-dismiss]").forEach(setupAutoDismiss);

  /* HTMX-injected flash messages (OOB swaps into #flash-region) */
  document.addEventListener("htmx:afterSettle", function (evt) {
    var target = evt.detail.target;
    if (target && target.id === "flash-region") {
      target.querySelectorAll("[data-auto-dismiss]").forEach(setupAutoDismiss);
    }
  });

  /* Copy-to-clipboard — [data-copy-url] buttons */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-url]");
    if (!btn) return;
    navigator.clipboard.writeText(btn.dataset.copyUrl).then(
      function () {
        var orig = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = orig; }, 2000);
        showFlash("success", "URL copied to clipboard.");
      },
      function () {
        showFlash("error", "Copy failed — please copy the URL manually.");
      }
    );
  });
})();
