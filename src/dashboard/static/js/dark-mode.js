/**
 * Dark mode toggle — reads/writes localStorage key "watcher-color-scheme".
 * Requires: button#theme-toggle with child span[data-theme-icon].
 */
(function () {
  var KEY = "watcher-color-scheme";
  var btn = document.getElementById("theme-toggle");
  if (!btn) return;

  var icon = btn.querySelector("[data-theme-icon]");

  function isDark() {
    return document.documentElement.classList.contains("dark");
  }

  function update() {
    var dark = isDark();
    if (icon) icon.textContent = dark ? "\u2600" : "\u263D";
    btn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  }

  btn.addEventListener("click", function () {
    var html = document.documentElement;
    var nowDark = html.classList.toggle("dark");
    localStorage.setItem(KEY, nowDark ? "dark" : "light");
    update();
    document.dispatchEvent(new CustomEvent("watcher:theme-changed", { detail: { theme: nowDark ? "dark" : "light" } }));
  });

  update();
})();
