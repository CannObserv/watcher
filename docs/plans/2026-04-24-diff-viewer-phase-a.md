# Watch Change Diff Viewer — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the `src/core/diff/` foundation and swap the Watch Change detail diff renderer from a bespoke side-by-side table to **diff2html-ui**, delivering GitHub-style collapsible context, word-level highlights inside changed lines, an inline ↔ side-by-side toggle, and syntax highlighting — without introducing `xmldiff` (Phase B) or touching notifications (issue #116).

**Architecture:** A new `src/core/diff/` module centralises diff computation in a three-stage pipeline (normalize → compute → render) reusable by dashboard and future notifications. Phase A only exercises `normalize_text` + `compute_unified_diff`; `normalize_html` and `structural.py` ship as stubs for Phase B. The dashboard stops building `(tag, prev, curr)` line tuples and instead ships a standard unified-diff string to the browser, where a vendored **diff2html-ui** widget renders it. Word-level highlighting, context folding, and inline/side-by-side toggle are all provided by diff2html-ui's built-in options — no `diff-match-patch` dependency needed in Phase A. A third "Structure" segment is added to the mode toggle but rendered disabled.

**Tech Stack:** Python `difflib.unified_diff` (stdlib), `diff2html` + `diff2html-ui` 3.4.x (vendored JS/CSS, no CDN), `highlight.js` bundled with diff2html, FastAPI + Jinja2 + HTMX + Alpine (existing stack), pytest TDD.

**Related:**
- Issue: [#115](https://github.com/CannObserv/watcher/issues/115) (this plan is Phase A)
- Follow-ups: #116 (notifications), #117 (tables/PDF)
- Existing code under replacement:
  - [src/dashboard/context.py:238-272](src/dashboard/context.py#L238-L272) — `generate_diff`
  - [src/dashboard/templates/partials/diff_view.html](src/dashboard/templates/partials/diff_view.html) — old side-by-side table
  - [src/dashboard/routes.py:2457-2500](src/dashboard/routes.py#L2457-L2500) — `/changes/{id}` and `/partials/diff/{id}`

---

## File Map

**Create:**
- `src/core/diff/__init__.py` — public exports
- `src/core/diff/models.py` — `DiffResult`, `NormalizeKind` dataclasses/enums
- `src/core/diff/normalize.py` — `normalize_text`; `normalize_html` (Phase B stub)
- `src/core/diff/textual.py` — `compute_unified_diff`
- `src/core/diff/structural.py` — Phase B stub (raises `NotImplementedError`)
- `src/dashboard/static/js/vendor/diff2html-ui.min.js` — vendored asset
- `src/dashboard/static/css/vendor/diff2html.min.css` — vendored asset
- `src/dashboard/static/js/diff-viewer.js` — page-scoped initializer
- `tests/core/diff/__init__.py` — test package marker
- `tests/core/diff/test_models.py`
- `tests/core/diff/test_normalize.py`
- `tests/core/diff/test_textual.py`
- `tests/core/diff/test_structural.py`

**Modify:**
- `src/dashboard/context.py` — delete `generate_diff`; drop `import difflib`
- `src/dashboard/routes.py:57` — drop `generate_diff` import
- `src/dashboard/routes.py:2457-2500` — build unified diff via `src.core.diff.textual.compute_unified_diff`, pass unified-diff string + `has_changes` bool into template context
- `src/dashboard/templates/partials/diff_view.html` — replace side-by-side table with a `<div data-unified-diff=...>` mount point
- `src/dashboard/templates/pages/change_detail.html` — add disabled "Structure" segment; include diff2html CSS + diff-viewer.js
- `tests/dashboard/test_context.py:465-481` — delete `TestGenerateDiff` class

**Do NOT touch in Phase A:**
- Anything under `src/core/notifications/` (reserved for #116)
- `xmldiff` integration (reserved for Phase B)
- `diff-match-patch` (not needed; diff2html-ui provides word-level via `matching: 'words'`)

---

## Pre-flight: environment + asset download

These aren't code tasks but must happen before Task 7.

- [ ] **P1: Confirm env loads and tests pass on main**

  ```bash
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
  uv run pytest tests/dashboard/test_context.py -v
  ```
  Expected: existing `TestGenerateDiff` tests pass. Baseline before edits.

- [ ] **P2: Download diff2html-ui bundle into vendor/**

  ```bash
  mkdir -p src/dashboard/static/js/vendor src/dashboard/static/css/vendor
  curl -sSL -o src/dashboard/static/js/vendor/diff2html-ui.min.js \
    https://cdn.jsdelivr.net/npm/diff2html@3.4.52/bundles/js/diff2html-ui.min.js
  curl -sSL -o src/dashboard/static/css/vendor/diff2html.min.css \
    https://cdn.jsdelivr.net/npm/diff2html@3.4.52/bundles/css/diff2html.min.css
  ls -la src/dashboard/static/js/vendor/diff2html-ui.min.js \
         src/dashboard/static/css/vendor/diff2html.min.css
  ```
  Expected: both files present, non-zero size (JS ~200KB, CSS ~20KB). The UI bundle includes `diff2html` core and embeds highlight.js — no extra downloads needed.

  **Pin the version.** The URL above locks `3.4.52`. If a newer point release exists, prefer it only if changelog shows non-breaking changes. Record the version in a brief top-of-file comment in `diff-viewer.js`.

---

## Task 1: `DiffResult` dataclass and module skeleton

**Files:**
- Create: `src/core/diff/__init__.py`
- Create: `src/core/diff/models.py`
- Create: `tests/core/diff/__init__.py`
- Create: `tests/core/diff/test_models.py`

- [ ] **Step 1: Write failing test**

  ```python
  # tests/core/diff/test_models.py
  """Tests for diff result DTOs."""

  from src.core.diff.models import DiffResult


  class TestDiffResult:
      def test_construct_empty(self):
          r = DiffResult(unified_diff="", has_changes=False, added=0, removed=0)
          assert r.unified_diff == ""
          assert r.has_changes is False
          assert r.added == 0
          assert r.removed == 0

      def test_construct_with_changes(self):
          r = DiffResult(
              unified_diff="--- previous\n+++ current\n@@ -1 +1 @@\n-a\n+b\n",
              has_changes=True,
              added=1,
              removed=1,
          )
          assert r.has_changes is True
          assert "@@" in r.unified_diff

      def test_is_frozen(self):
          """DiffResult is immutable — safe to share across templates."""
          import dataclasses

          r = DiffResult(unified_diff="", has_changes=False, added=0, removed=0)
          try:
              r.added = 5  # type: ignore[misc]
          except dataclasses.FrozenInstanceError:
              return
          raise AssertionError("DiffResult should be frozen")
  ```

- [ ] **Step 2: Run test to confirm failure**

  ```bash
  uv run pytest tests/core/diff/test_models.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'src.core.diff'`.

- [ ] **Step 3: Create module skeleton and `DiffResult`**

  ```python
  # src/core/diff/__init__.py
  """Diff service: normalize → compute → render.

  Shared by dashboard change-detail views and (future) notification output.
  See docs/plans/2026-04-24-diff-viewer-phase-a.md and issues #115/#116/#117.
  """

  from src.core.diff.models import DiffResult

  __all__ = ["DiffResult"]
  ```

  ```python
  # src/core/diff/models.py
  """DTOs for the diff service."""

  from dataclasses import dataclass


  @dataclass(frozen=True, slots=True)
  class DiffResult:
      """Outcome of a textual diff.

      unified_diff — standard unified-diff text (empty if no changes).
      has_changes  — true iff at least one line was added or removed.
      added        — count of added lines.
      removed      — count of removed lines.
      """

      unified_diff: str
      has_changes: bool
      added: int
      removed: int
  ```

  Also create the empty `tests/core/diff/__init__.py`.

- [ ] **Step 4: Run tests to confirm pass**

  ```bash
  uv run pytest tests/core/diff/test_models.py -v
  ```
  Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

  ```bash
  uv run ruff check src/core/diff/ tests/core/diff/
  git add src/core/diff/ tests/core/diff/__init__.py tests/core/diff/test_models.py
  git commit -m "#115 feat: add src/core/diff module skeleton with DiffResult"
  ```

---

## Task 2: `normalize_text` and `normalize_html` stub

**Files:**
- Create: `src/core/diff/normalize.py`
- Create: `tests/core/diff/test_normalize.py`

`normalize_text` canonicalises newlines and strips per-line trailing whitespace so spurious whitespace changes don't inflate the diff. `normalize_html` is a deliberate Phase A passthrough — it has the same signature it will have in Phase B so callers don't need to change when xmldiff arrives.

- [ ] **Step 1: Write failing tests**

  ```python
  # tests/core/diff/test_normalize.py
  """Tests for the normalization stage."""

  from src.core.diff.normalize import normalize_html, normalize_text


  class TestNormalizeText:
      def test_passthrough_lf(self):
          assert normalize_text("a\nb\nc") == "a\nb\nc"

      def test_crlf_to_lf(self):
          assert normalize_text("a\r\nb\r\nc") == "a\nb\nc"

      def test_cr_to_lf(self):
          assert normalize_text("a\rb\rc") == "a\nb\nc"

      def test_strip_trailing_whitespace(self):
          assert normalize_text("a   \nb\t\nc") == "a\nb\nc"

      def test_preserves_leading_whitespace(self):
          assert normalize_text("  indent\n\tindent") == "  indent\n\tindent"

      def test_preserves_blank_lines(self):
          assert normalize_text("a\n\nb") == "a\n\nb"

      def test_empty(self):
          assert normalize_text("") == ""


  class TestNormalizeHtml:
      def test_phase_a_is_passthrough(self):
          """Phase A stub: returns input unchanged. Phase B (xmldiff) replaces this."""
          src = "<div>  <p>hi</p>  </div>"
          assert normalize_html(src) == src

      def test_empty(self):
          assert normalize_html("") == ""
  ```

- [ ] **Step 2: Run to confirm failure**

  ```bash
  uv run pytest tests/core/diff/test_normalize.py -v
  ```
  Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

  ```python
  # src/core/diff/normalize.py
  """Normalization stage: canonicalise input so diffs carry signal, not noise.

  - normalize_text: canonicalise line endings + strip trailing whitespace per line.
  - normalize_html: Phase A passthrough. Phase B replaces with lxml/html5lib
    pretty-print + attribute sort + comment strip (see issue #115).
  """


  def normalize_text(text: str) -> str:
      """Canonicalise newlines (CRLF/CR → LF) and strip per-line trailing whitespace."""
      if not text:
          return text
      unified = text.replace("\r\n", "\n").replace("\r", "\n")
      return "\n".join(line.rstrip() for line in unified.split("\n"))


  def normalize_html(html: str) -> str:
      """Phase A stub: passthrough. Phase B will add structural normalization."""
      return html
  ```

- [ ] **Step 4: Run tests**

  ```bash
  uv run pytest tests/core/diff/test_normalize.py -v
  ```
  Expected: 9 passed.

- [ ] **Step 5: Commit**

  ```bash
  uv run ruff check src/core/diff/ tests/core/diff/
  git add src/core/diff/normalize.py tests/core/diff/test_normalize.py
  git commit -m "#115 feat: add normalize_text and normalize_html (Phase B stub)"
  ```

---

## Task 3: `compute_unified_diff`

**Files:**
- Create: `src/core/diff/textual.py`
- Create: `tests/core/diff/test_textual.py`
- Modify: `src/core/diff/__init__.py` — export `compute_unified_diff`

Use `difflib.unified_diff` — stdlib, deterministic, well-understood. Call `normalize_text` first so whitespace noise is squashed. Return a frozen `DiffResult`.

- [ ] **Step 1: Write failing tests**

  ```python
  # tests/core/diff/test_textual.py
  """Tests for compute_unified_diff."""

  from src.core.diff.textual import compute_unified_diff


  class TestComputeUnifiedDiff:
      def test_identical_text_has_no_changes(self):
          r = compute_unified_diff("hello\nworld\n", "hello\nworld\n")
          assert r.has_changes is False
          assert r.unified_diff == ""
          assert r.added == 0
          assert r.removed == 0

      def test_simple_modification(self):
          r = compute_unified_diff("hello\nworld\n", "hello\nplanet\n")
          assert r.has_changes is True
          assert "-world" in r.unified_diff
          assert "+planet" in r.unified_diff
          assert r.added == 1
          assert r.removed == 1

      def test_pure_addition(self):
          r = compute_unified_diff("a\n", "a\nb\n")
          assert r.has_changes is True
          assert r.added == 1
          assert r.removed == 0

      def test_pure_deletion(self):
          r = compute_unified_diff("a\nb\n", "a\n")
          assert r.has_changes is True
          assert r.added == 0
          assert r.removed == 1

      def test_header_labels_are_stable(self):
          """Unified diff uses fixed 'previous' / 'current' labels (not filenames)."""
          r = compute_unified_diff("a\n", "b\n")
          assert r.unified_diff.startswith("--- previous\n+++ current\n")

      def test_whitespace_only_change_is_normalized_away(self):
          """Trailing whitespace differences should not show as changes."""
          r = compute_unified_diff("hello   \nworld\n", "hello\nworld\n")
          assert r.has_changes is False

      def test_crlf_vs_lf_is_normalized_away(self):
          r = compute_unified_diff("a\r\nb\r\n", "a\nb\n")
          assert r.has_changes is False

      def test_empty_both(self):
          r = compute_unified_diff("", "")
          assert r.has_changes is False
          assert r.unified_diff == ""

      def test_empty_previous(self):
          r = compute_unified_diff("", "new line\n")
          assert r.has_changes is True
          assert r.added == 1

      def test_context_lines_included(self):
          """Default context lines means surrounding unchanged lines appear in output."""
          prev = "a\nb\nc\nd\ne\n"
          curr = "a\nb\nX\nd\ne\n"
          r = compute_unified_diff(prev, curr, context=3)
          # Context should include 'b' before and 'd' after the change.
          assert " b" in r.unified_diff
          assert " d" in r.unified_diff

      def test_custom_context(self):
          r0 = compute_unified_diff("a\nb\nc\nX\ne\nf\ng\n", "a\nb\nc\nY\ne\nf\ng\n", context=0)
          r3 = compute_unified_diff("a\nb\nc\nX\ne\nf\ng\n", "a\nb\nc\nY\ne\nf\ng\n", context=3)
          # More context ⇒ more total lines in the diff body.
          assert len(r3.unified_diff.splitlines()) > len(r0.unified_diff.splitlines())
  ```

- [ ] **Step 2: Run to confirm failure**

  ```bash
  uv run pytest tests/core/diff/test_textual.py -v
  ```
  Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

  ```python
  # src/core/diff/textual.py
  """Textual diff computation (stdlib difflib, unified format)."""

  import difflib

  from src.core.diff.models import DiffResult
  from src.core.diff.normalize import normalize_text

  _DEFAULT_CONTEXT = 3


  def compute_unified_diff(
      previous: str, current: str, *, context: int = _DEFAULT_CONTEXT
  ) -> DiffResult:
      """Compute a unified diff between two text strings.

      Normalizes both sides (CRLF→LF, trailing whitespace) before diffing so
      that whitespace-only differences collapse to has_changes=False.
      """
      prev_norm = normalize_text(previous)
      curr_norm = normalize_text(current)

      if prev_norm == curr_norm:
          return DiffResult(unified_diff="", has_changes=False, added=0, removed=0)

      prev_lines = prev_norm.splitlines(keepends=True)
      curr_lines = curr_norm.splitlines(keepends=True)

      hunks = difflib.unified_diff(
          prev_lines,
          curr_lines,
          fromfile="previous",
          tofile="current",
          n=context,
          lineterm="",
      )
      text = "\n".join(hunks)

      added = 0
      removed = 0
      for line in text.splitlines():
          if line.startswith("+++") or line.startswith("---"):
              continue
          if line.startswith("+"):
              added += 1
          elif line.startswith("-"):
              removed += 1

      return DiffResult(
          unified_diff=text,
          has_changes=(added > 0 or removed > 0),
          added=added,
          removed=removed,
      )
  ```

- [ ] **Step 4: Update `__init__.py` to re-export**

  ```python
  # src/core/diff/__init__.py
  """Diff service: normalize → compute → render."""

  from src.core.diff.models import DiffResult
  from src.core.diff.textual import compute_unified_diff

  __all__ = ["DiffResult", "compute_unified_diff"]
  ```

- [ ] **Step 5: Run tests**

  ```bash
  uv run pytest tests/core/diff/ -v
  ```
  Expected: all tests pass (Task 1 + 2 + 3 combined).

- [ ] **Step 6: Commit**

  ```bash
  uv run ruff check src/core/diff/ tests/core/diff/
  git add src/core/diff/ tests/core/diff/test_textual.py
  git commit -m "#115 feat: add compute_unified_diff with normalization"
  ```

---

## Task 4: `structural.py` Phase B stub

**Files:**
- Create: `src/core/diff/structural.py`
- Create: `tests/core/diff/test_structural.py`

The stub exists so routes can import a stable name now. Phase B fills it in.

- [ ] **Step 1: Write failing test**

  ```python
  # tests/core/diff/test_structural.py
  """Tests for structural HTML diff (Phase B stub in Phase A)."""

  import pytest

  from src.core.diff.structural import compute_html_tree_diff


  class TestComputeHtmlTreeDiffStub:
      def test_raises_not_implemented(self):
          with pytest.raises(NotImplementedError, match="Phase B"):
              compute_html_tree_diff("<p>a</p>", "<p>b</p>")
  ```

- [ ] **Step 2: Run to confirm failure**

  ```bash
  uv run pytest tests/core/diff/test_structural.py -v
  ```
  Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement stub**

  ```python
  # src/core/diff/structural.py
  """Structural (tree) diff for HTML.

  Phase A: stub. Phase B replaces with xmldiff-based tree edit-distance diff.
  See issue #115 for the rollout plan.
  """


  def compute_html_tree_diff(previous: str, current: str) -> list:
      """Compute a structural diff between two HTML documents.

      Phase A stub — always raises. Phase B implements via `xmldiff`.
      """
      raise NotImplementedError(
          "compute_html_tree_diff is reserved for issue #115 Phase B (xmldiff)"
      )
  ```

- [ ] **Step 4: Run tests**

  ```bash
  uv run pytest tests/core/diff/ -v
  ```
  Expected: all tests pass.

- [ ] **Step 5: Commit**

  ```bash
  uv run ruff check src/core/diff/ tests/core/diff/
  git add src/core/diff/structural.py tests/core/diff/test_structural.py
  git commit -m "#115 feat: add structural.py Phase B stub"
  ```

---

## Task 5: Remove `generate_diff` and wire routes to `compute_unified_diff`

**Files:**
- Modify: `src/dashboard/context.py` — delete `generate_diff` function and `import difflib`
- Modify: `src/dashboard/routes.py` — drop `generate_diff` import; update both routes to build a unified diff via `compute_unified_diff`; pass `diff: DiffResult` into template context
- Modify: `tests/dashboard/test_context.py` — delete `TestGenerateDiff` class and the `generate_diff` import

This is the swing cut. After this task the page still works but the template still expects old `diff.lines` — we'll fix the template in Task 6. Accept that the dashboard change-detail view is **broken between Task 5 and Task 6**; commit the two together if running in one session, or land Task 5 behind a `pytest -m integration` skip if you prefer.

- [ ] **Step 1: Write failing test (route integration)**

  ```python
  # tests/dashboard/test_change_detail_route.py  (NEW FILE)
  """Smoke test: change detail page renders with a unified-diff string in context."""

  import pytest

  pytestmark = pytest.mark.integration


  async def test_change_detail_passes_unified_diff(client, sample_change_with_snapshots):
      """Route provides diff.unified_diff (not diff.lines) to the template."""
      response = await client.get(f"/changes/{sample_change_with_snapshots.id}")
      assert response.status_code == 200
      # The new template emits a mount point with the unified diff attribute.
      assert b"data-unified-diff" in response.content
  ```

  **Note:** `sample_change_with_snapshots` fixture may need adding to `tests/conftest.py` if absent. Check first with `grep -n "sample_change_with_snapshots" tests/conftest.py tests/dashboard/conftest.py`; if missing, build a minimal one that inserts a Watch + two Snapshots + a Change with known text content. If fixture scaffolding is too large for this task, drop to a unit test on `context.py` shape instead and mark the integration test `xfail` pending Task 6.

- [ ] **Step 2: Run and confirm failure**

  ```bash
  uv run pytest tests/dashboard/test_change_detail_route.py -v
  ```
  Expected: fails — route still returns old template shape.

- [ ] **Step 3: Remove `generate_diff` from context.py**

  Delete lines `237-272` ([src/dashboard/context.py:237-272](src/dashboard/context.py#L237-L272)) entirely. Remove the `import difflib` at line 3. Leave `summarize_change_metadata` untouched.

- [ ] **Step 4: Update change-detail routes**

  In `src/dashboard/routes.py` line 57, drop `generate_diff` from the `from src.dashboard.context import (...)` block.

  Add a new import near the other `src.core` imports:

  ```python
  from src.core.diff import compute_unified_diff
  ```

  Replace the body of `change_detail_page` (lines 2457-2480) and `partial_diff` (lines 2483-2500) so both call `compute_unified_diff(prev_text, curr_text)` and pass the resulting `DiffResult` into the template under the name `diff`.

  ```python
  # change_detail_page: replace `diff = generate_diff(...)` with:
  diff = compute_unified_diff(prev_text, curr_text)
  # partial_diff: same change; the returned TemplateResponse keeps {"diff": diff}.
  ```

- [ ] **Step 5: Delete old tests**

  In `tests/dashboard/test_context.py`:
  - Drop `generate_diff` from the `from src.dashboard.context import (...)` block at line 13.
  - Delete the entire `TestGenerateDiff` class (lines 465-481).

- [ ] **Step 6: Run route + context test suites**

  ```bash
  uv run pytest tests/dashboard/test_context.py tests/dashboard/test_change_detail_route.py -v
  ```
  Expected: `test_context.py` passes; the new integration test still fails — this is expected; Task 6 fixes the template.

- [ ] **Step 7: Do NOT commit yet** — the template is about to change. Proceed to Task 6, then commit the two together.

---

## Task 6: Replace `diff_view.html` with diff2html mount point

**Files:**
- Modify: `src/dashboard/templates/partials/diff_view.html` — replace contents wholesale
- Modify: `src/dashboard/templates/pages/change_detail.html` — load CSS + JS on this page only; add disabled "Structure" segment

The unified-diff string is rendered by diff2html-ui on the client. The partial becomes a thin mount point plus a "No changes" fallback.

- [ ] **Step 1: Rewrite the partial**

  ```html
  {# src/dashboard/templates/partials/diff_view.html #}
  {% if diff.has_changes %}
    <div
      class="diff-mount border border-gray-200 dark:border-gray-700 rounded"
      data-unified-diff="{{ diff.unified_diff }}"
      data-output-format="side-by-side"
      aria-label="File diff"
    >
      {# diff-viewer.js renders diff2html-ui into this container on load / HTMX swap #}
    </div>
  {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">No textual differences found.</p>
  {% endif %}
  ```

  **Note on escaping:** Jinja auto-escapes `{{ diff.unified_diff }}` inside an attribute, which is exactly what we want — the unified diff is opaque text to the browser until `diff-viewer.js` picks it up.

- [ ] **Step 2: Update `change_detail.html`**

  Add above the existing diff block (or into the `{% block head_extra %}` if present — if not, add inline in the body before `<div id="diff-content">`):

  ```html
  <link rel="stylesheet" href="/static/css/vendor/diff2html.min.css?v={{ build_id }}">
  ```

  Add after the existing `<script src="/static/js/notifications.js">` block, or at the bottom of the change_detail content block:

  ```html
  <script src="/static/js/vendor/diff2html-ui.min.js?v={{ build_id }}" defer></script>
  <script src="/static/js/diff-viewer.js?v={{ build_id }}" defer></script>
  ```

  **Important:** These go on the change_detail page only, not in `base.html`. diff2html is ~200KB gzipped — loading it globally is waste.

  In the same file, add a third segment to the radiogroup at [change_detail.html:118-135](src/dashboard/templates/pages/change_detail.html#L118-L135), disabled with a title attribute explaining:

  ```html
  <label class="segment segment-disabled" aria-disabled="true">
    <input type="radio" name="mode" value="structure" disabled
      aria-describedby="structure-coming-soon-hint">
    <span>Structure</span>
  </label>
  <span id="structure-coming-soon-hint" class="sr-only">Structural diff coming in Phase B (issue #115)</span>
  ```

  **Check `.segment-disabled` exists** in [src/dashboard/static/css/input.css](src/dashboard/static/css/input.css). If it doesn't, add:

  ```css
  .segment-disabled {
    @apply opacity-50 cursor-not-allowed;
  }
  .segment-disabled:hover {
    @apply bg-transparent dark:bg-transparent;
  }
  ```

  Then rebuild CSS: `bash scripts/build-css.sh`.

  (AGENTS.md forbids `title` attributes for a11y reasons — use the hint span pattern above.)

- [ ] **Step 3: Create `diff-viewer.js`**

  ```javascript
  // src/dashboard/static/js/diff-viewer.js
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

    // HTMX: re-render after swap into #diff-content when Raw/Extracted toggled.
    document.body.addEventListener("htmx:afterSwap", function (e) {
      renderAll(e.target);
    });

    // Mode-toggle fallback (non-HTMX): initial radio selection doesn't fire change.
    // Nothing to do — each HTMX request re-fetches the partial which re-mounts.
  })();
  ```

- [ ] **Step 4: Run the failing integration test from Task 5**

  ```bash
  uv run pytest tests/dashboard/test_change_detail_route.py -v
  ```
  Expected: now passes — `data-unified-diff` is in the response body.

- [ ] **Step 5: Full test suite**

  ```bash
  uv run pytest -v
  ```
  Expected: all green. If any test imported `generate_diff`, update it. If `tests/dashboard/` fixtures rely on the old `diff.lines` shape, migrate them.

- [ ] **Step 6: Lint**

  ```bash
  uv run ruff check src/ tests/
  ```

- [ ] **Step 7: Build CSS if segment-disabled was added**

  ```bash
  bash scripts/build-css.sh
  ```

- [ ] **Step 8: Commit Tasks 5 + 6 together**

  ```bash
  git add src/dashboard/context.py src/dashboard/routes.py \
          src/dashboard/templates/partials/diff_view.html \
          src/dashboard/templates/pages/change_detail.html \
          src/dashboard/static/js/diff-viewer.js \
          src/dashboard/static/js/vendor/diff2html-ui.min.js \
          src/dashboard/static/css/vendor/diff2html.min.css \
          src/dashboard/static/css/input.css src/dashboard/static/css/output.css \
          tests/dashboard/test_context.py \
          tests/dashboard/test_change_detail_route.py
  git commit -m "#115 feat: render change-detail diff with diff2html-ui"
  ```

---

## Task 7: Browser smoke test on port 8001

Mission-critical UI change — type checking is not enough. Per AGENTS.md, run the dev server and exercise the feature.

- [ ] **Step 1: Start dev server**

  ```bash
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
  uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
  ```
  (Use a background shell or a second terminal. Leave running until Task 7 finishes.)

- [ ] **Step 2: Find a change to exercise**

  ```bash
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
  uv run python -c "
  import asyncio
  from sqlalchemy import select
  from src.api.db import get_session_factory
  from src.core.models.change import Change

  async def main():
      async with get_session_factory()() as s:
          r = await s.execute(select(Change.id).limit(3))
          for (cid,) in r:
              print(cid)
  asyncio.run(main())
  "
  ```

  If no changes exist yet, either run a watch through the pipeline to generate one, or pick a seed change from a fresh test DB via `uv run pytest --setup-show tests/workers/test_pipeline.py -v -k integration`.

- [ ] **Step 3: Visit in browser, exercise every interaction**

  Visit `https://watcher.exe.xyz:8001/changes/<id>`.

  Confirm:
  - [ ] Page loads without console errors (DevTools open throughout)
  - [ ] Diff renders in side-by-side mode by default
  - [ ] Changed lines show word-level highlighting (the substring that changed within a line is visually distinct)
  - [ ] Unchanged context lines appear around hunks; a "Expand N lines" affordance is visible between non-adjacent hunks
  - [ ] Switching **Extracted Text ↔ Raw Content** via the radio fetches a new partial via HTMX and re-renders diff2html
  - [ ] **Structure** segment is visibly disabled and cannot be toggled; screen reader announces "coming soon"
  - [ ] Dark mode renders correctly — switch themes and confirm the diff is still readable
  - [ ] The "No textual differences found" fallback shows when previous == current (find or fabricate a change with identical extracted text; or temporarily return equal strings from `_load_snapshot_text` to verify)
  - [ ] Visual comparison (screenshots) block still renders above the diff, unchanged
  - [ ] No regressions on the rest of the change_detail page (chunk table, metadata, snapshot info)

- [ ] **Step 4: Accessibility pass**

  - [ ] Skip link + keyboard-only nav still works
  - [ ] Radio group is reachable via Tab; disabled option is announced
  - [ ] `focus-visible` ring appears on diff2html interactive elements (Expand affordance)
  - [ ] `prefers-reduced-motion` — confirm no janky transitions

- [ ] **Step 5: Report results**

  Stop the dev server. If anything failed, create a follow-up task (or fix in place if small). If everything passes, move to Task 8.

---

## Task 8: Restart systemd and close the loop

- [ ] **Step 1: Confirm main is clean and pushed**

  ```bash
  git status
  git log --oneline -5
  ```
  Expected: Tasks 1-6 commits visible; working tree clean.

- [ ] **Step 2: Restart live service**

  ```bash
  sudo systemctl restart watcher
  sudo systemctl status watcher --no-pager | head -20
  ```
  Expected: active (running).

- [ ] **Step 3: Hit a live change in production**

  ```bash
  curl -sSI https://watcher.exe.xyz/changes/<id> | head -5
  ```
  Or open the URL in a browser. Confirm diff renders.

- [ ] **Step 4: Update #115**

  ```bash
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
  gh issue comment 115 --body "Phase A landed — src/core/diff/ foundation + diff2html-ui on change detail. Priorities 1-4 delivered. Phase B (xmldiff structural diff + normalize_html) up next."
  ```

  Do not close the issue — Phases B/C/D still to land.

---

## Appendix A: Decisions and non-obvious calls

- **Why stdlib `difflib.unified_diff` (not `diff-match-patch`)?** diff2html-ui does word/char-level highlighting natively via `matching: 'words'` + JsDiff. Adding `diff-match-patch` in Phase A duplicates that work. Revisit for notifications (#116) where we render HTML server-side and may want finer control.
- **Why vendor diff2html-ui instead of CDN?** AGENTS.md: "Pre-built Tailwind (no CDN)." Same principle applies to JS — no runtime dependency on a third-party CDN for production assets.
- **Why not load diff2html in base.html?** It's ~200KB gzipped. Only the change-detail page needs it. The four other dashboard pages shouldn't pay that cost.
- **Why a disabled Structure segment now?** Signals forthcoming capability to users and to ourselves; prevents template rework when Phase B lands.
- **Why normalize whitespace in `compute_unified_diff`?** Raw HTML/text often has trailing whitespace or CRLF noise that produces false positives. Signal over noise is the whole point of this issue.
- **Why freeze `DiffResult`?** Immutability + `slots=True` is cheap and avoids accidental template mutation issues.

## Appendix B: Files NOT changed in Phase A (and why)

- `src/core/notifications/*` — issue #116.
- `src/core/diff/render.py` — not needed; rendering is client-side via diff2html-ui in Phase A. May be added in #116 for server-side HTML notification rendering.
- `pyproject.toml` — no new Python dependencies in Phase A. diff-match-patch deferred to #116 (if needed). xmldiff/lxml (already present) used in Phase B.
- `alembic/` — no schema changes.

## Appendix C: Rollback

If Phase A ships broken:

```bash
git revert <commit-sha-of-task-6-commit>
git revert <commit-sha-of-task-3-commit>  # etc.
sudo systemctl restart watcher
```

The old `generate_diff` + old template can be restored from git history. No DB state touched.
