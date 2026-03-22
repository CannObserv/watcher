/**
 * watcher dashboard — custom JS
 */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-auto-dismiss]").forEach(function (el) {
    setTimeout(function () {
      el.remove();
    }, 5000);
  });
});

function toggleDiffView(mode) {
  document.querySelectorAll("[data-diff-view]").forEach(function (el) {
    el.classList.toggle("hidden", el.dataset.diffView !== mode);
  });
  document.querySelectorAll("[data-diff-toggle]").forEach(function (btn) {
    btn.classList.toggle("bg-gray-200", btn.dataset.diffToggle === mode);
    btn.classList.toggle("bg-white", btn.dataset.diffToggle !== mode);
  });
}
