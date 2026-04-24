// diff2html-ui 3.4.52 — renders unified diff from data-unified-diff attribute.
// Activates on DOMContentLoaded and on HTMX content swaps into #diff-content.

(function () {
  "use strict";

  function currentColorScheme() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  function render(el) {
    if (!el) return;
    var diffText = el.dataset.unifiedDiff || "";
    if (!diffText) return;
    // Graceful degradation: if the vendor bundle failed to load (404, blocked,
    // offline), show a styled message instead of an empty bordered frame.
    if (typeof Diff2HtmlUI === "undefined") {
      el.innerHTML =
        '<p class="p-4 text-sm text-gray-500 dark:text-gray-400">Diff viewer failed to load.</p>';
      return;
    }
    // Clear previous render so re-draws don't stack content.
    el.innerHTML = "";
    // eslint-disable-next-line no-undef
    var ui = new Diff2HtmlUI(el, diffText, {
      outputFormat: el.dataset.outputFormat || "side-by-side",
      drawFileList: false,
      matching: "words",
      matchWordsThreshold: 0.25,
      highlight: true,
      renderNothingWhenEmpty: false,
      colorScheme: currentColorScheme(),
    });
    ui.draw();
    ui.highlightCode();
  }

  function renderAll(root) {
    (root || document).querySelectorAll(".diff-mount").forEach(render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { renderAll(); });
  } else {
    renderAll();
  }

  // Re-render when the theme toggles (emitted by dark-mode.js).
  document.addEventListener("watcher:theme-changed", function () {
    renderAll();
  });

  // HTMX: re-render after swap (Raw/Extracted mode toggle).
  // Only act when the swap target is or contains #diff-content.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "diff-content" || (t.querySelector && t.querySelector("#diff-content"))) {
      renderAll(t);
    }
  });
})();
