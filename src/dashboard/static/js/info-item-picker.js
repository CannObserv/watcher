/* InfoItem picker keyboard navigation.
 *
 * Wires `.info-item-picker` containers. Arrow keys move
 * aria-activedescendant across options; Enter activates the highlighted
 * option; Escape clears.
 *
 * After the Task 6 CR fix, options are <button role="option"> elements
 * directly (no <li> wrapper carrying the role), so activate by clicking
 * the matched element.
 */
(function () {
  function wire(picker) {
    var input = picker.querySelector('input[role="combobox"]');
    if (!input) return;
    var resultsId = input.getAttribute('aria-controls');
    if (!resultsId) return;
    var treeEl = picker.querySelector('[id$="-binding-tree"]');
    var treeId = treeEl ? treeEl.id : null;

    var activeIdx = -1;

    function options() {
      var region = document.getElementById(resultsId);
      return region ? region.querySelectorAll('[role="option"]') : [];
    }

    function highlight(idx) {
      var opts = options();
      if (!opts.length) {
        activeIdx = -1;
        input.setAttribute('aria-activedescendant', '');
        return;
      }
      if (idx < 0) {
        activeIdx = -1;
        opts.forEach(function (o) { o.setAttribute('aria-selected', 'false'); });
        input.setAttribute('aria-activedescendant', '');
        return;
      }
      activeIdx = Math.min(opts.length - 1, idx);
      opts.forEach(function (o, i) {
        o.setAttribute('aria-selected', i === activeIdx ? 'true' : 'false');
      });
      input.setAttribute('aria-activedescendant', opts[activeIdx].id || '');
    }

    input.addEventListener('keydown', function (e) {
      var opts = options();
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        highlight(activeIdx + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        highlight(activeIdx - 1);
      } else if (e.key === 'Enter') {
        if (activeIdx >= 0 && opts[activeIdx]) {
          e.preventDefault();
          opts[activeIdx].click();
        }
      } else if (e.key === 'Escape') {
        input.value = '';
        var region = document.getElementById(resultsId);
        if (region) region.innerHTML = '';
        highlight(-1);
        input.setAttribute('aria-expanded', 'false');
      }
    });

    /* Reset highlight after HTMX swaps in new results. */
    document.body.addEventListener('htmx:afterSwap', function (e) {
      var targetId = e.detail && e.detail.target && e.detail.target.id;
      if (targetId === resultsId) {
        activeIdx = -1;
        input.setAttribute('aria-expanded', options().length > 0 ? 'true' : 'false');
      }
      /* Clear the search input and results when a selection populates the binding tree. */
      if (treeId && targetId === treeId) {
        input.value = '';
        var region = document.getElementById(resultsId);
        if (region) region.innerHTML = '';
        highlight(-1);
        input.setAttribute('aria-expanded', 'false');
      }
    });
  }

  document.querySelectorAll('.info-item-picker').forEach(wire);
})();
