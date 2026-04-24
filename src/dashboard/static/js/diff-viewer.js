// diff2html-ui 3.4.52 — renders unified diff from data-unified-diff attribute.
// Activates on DOMContentLoaded and on HTMX content swaps into #diff-content.

(function () {
  "use strict";

  function render(el) {
    if (!el || el.dataset.rendered === "1") return;
    var diffText = el.dataset.unifiedDiff || "";
    if (!diffText) return;
    // diff2html-ui is exposed as global `Diff2HtmlUI`.
    // eslint-disable-next-line no-undef
    var ui = new Diff2HtmlUI(el, diffText, {
      outputFormat: el.dataset.outputFormat || "side-by-side",
      drawFileList: false,
      matching: "words",
      matchWordsThreshold: 0.25,
      highlight: true,
      renderNothingWhenEmpty: false,
    });
    ui.draw();
    ui.highlightCode();
    el.dataset.rendered = "1";
  }

  function renderAll(root) {
    (root || document).querySelectorAll(".diff-mount").forEach(render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { renderAll(); });
  } else {
    renderAll();
  }

  // HTMX: re-render after swap (Raw/Extracted mode toggle).
  document.body.addEventListener("htmx:afterSwap", function (e) {
    renderAll(e.target);
  });
})();
