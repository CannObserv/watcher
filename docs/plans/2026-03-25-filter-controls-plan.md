# Filter Controls Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `.filter-pill` buttons with a segmented control (single-select, radio-based) for status filters and a chip group (multi-select, checkbox-based) for event type filters.

**Architecture:** Two new CSS component families (`.segment-group`/`.segment` and `.chip-group`/`.chip`) in `input.css`, built on native `<input type="radio">` / `<input type="checkbox">` with `input:checked + span` for pure-CSS active states. Templates wrap controls in `<form method="get">` with `<noscript>` submit fallback. HTMX `hx-get` + `hx-trigger="change"` on the inputs for progressive enhancement.

**Tech Stack:** Tailwind v4 (`input.css`), Jinja2 templates, HTMX, bash (Tailwind build)

**Design doc:** `docs/plans/2026-03-25-filter-controls-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/dashboard/static/css/input.css` | Add `.segment-group`, `.segment`, `.chip-group`, `.chip`; remove `.filter-pill` |
| Modify | `src/dashboard/templates/pages/domains.html` | Replace filter-pill buttons with segmented control |
| Modify | `src/dashboard/templates/pages/watches.html` | Replace filter-pill buttons with segmented control |
| Modify | `src/dashboard/templates/pages/domain_detail.html` | Replace filter-pill buttons with segmented control |
| Modify | `src/dashboard/templates/pages/audit_log.html` | Replace filter-pill buttons with chip group |
| Modify | `src/dashboard/routes.py` | Pass `is_active` / `event_type` to template context for checked state |
| Modify | `docs/STYLE.md` | Replace Filter pill section with Segmented control + Chip group docs |
| Modify | `tests/dashboard/test_routes.py` | Add tests for filter control HTML structure |
| Rebuild | `src/dashboard/static/css/output.css` | Recompile Tailwind |

---

### Task 1: CSS — Add segment-group and segment components

**Files:**
- Modify: `src/dashboard/static/css/input.css:62-65`

- [ ] **Step 1: Write the segment-group and segment CSS**

Replace the `.filter-pill` block (lines 62–65) with the new components. Do NOT remove `.filter-pill` yet — other templates still reference it. Add the new classes directly after the `.btn-ghost` block:

```css
  /* -- Segmented control (single-select radio group) -- */
  .segment-group {
    @apply inline-flex border border-gray-300 dark:border-gray-600 rounded-lg overflow-hidden;
  }
  .segment {
    @apply relative cursor-pointer;
  }
  .segment input {
    @apply absolute opacity-0 w-0 h-0;
  }
  .segment span {
    @apply inline-flex items-center px-4 py-2 text-sm font-medium min-h-[44px] border-e border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 bg-transparent hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors select-none;
  }
  .segment:last-child span {
    @apply border-e-0;
  }
  .segment input:checked + span {
    @apply bg-co-purple-600 text-white dark:bg-co-purple-600 dark:text-white;
  }
  .segment input:focus-visible + span {
    @apply outline-2 outline-offset-2 outline-co-purple-600 dark:outline-co-purple-400;
  }
```

- [ ] **Step 2: Rebuild Tailwind and verify**

Run: `bash scripts/build-css.sh`
Expected: clean exit, `output.css` updated

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/static/css/input.css src/dashboard/static/css/output.css
git commit -m "#44 feat: add segment-group CSS component for single-select filters"
```

---

### Task 2: CSS — Add chip-group and chip components

**Files:**
- Modify: `src/dashboard/static/css/input.css`

- [ ] **Step 1: Add chip-group and chip CSS after segment components**

```css
  /* -- Chip group (multi-select checkbox group) -- */
  .chip-group {
    @apply flex flex-wrap gap-2;
  }
  .chip {
    @apply relative cursor-pointer;
  }
  .chip input {
    @apply absolute opacity-0 w-0 h-0;
  }
  .chip span {
    @apply inline-flex items-center px-3 py-1 text-sm rounded-full min-h-[44px] border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 bg-transparent hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors select-none;
  }
  .chip input:checked + span {
    @apply bg-co-purple-100 dark:bg-co-purple-800 border-co-purple-600 dark:border-co-purple-400 text-co-purple-700 dark:text-co-purple-400;
    /* Design spec called for co-purple-900/300; using 800/400 since those are the closest tokens defined in @theme */
  }
  .chip input:focus-visible + span {
    @apply outline-2 outline-offset-2 outline-co-purple-600 dark:outline-co-purple-400;
  }
```

- [ ] **Step 2: Rebuild Tailwind and verify**

Run: `bash scripts/build-css.sh`
Expected: clean exit

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/static/css/input.css src/dashboard/static/css/output.css
git commit -m "#44 feat: add chip-group CSS component for multi-select filters"
```

---

### Task 3: Template — Domains list segmented control

**Files:**
- Modify: `src/dashboard/templates/pages/domains.html:25-38`
- Modify: `src/dashboard/routes.py:302-332` (pass `status` for checked state — already done)

- [ ] **Step 1: Write failing test — domains page has radio inputs**

Add to `tests/dashboard/test_routes.py`:

```python
class TestDomainsPage:
    async def test_domains_page_returns_200(self, client):
        response = await client.get("/domains")
        assert response.status_code == 200
        assert b"Domains" in response.content

    async def test_domains_page_has_segment_control(self, client):
        response = await client.get("/domains")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="status"' in body
        assert b'type="radio"' in body

    async def test_domains_page_active_filter_checked(self, client):
        """Default status is 'active', so the active radio should be checked."""
        import re

        response = await client.get("/domains")
        body = response.text
        # The "active" radio should have the checked attribute
        assert re.search(r'value="active"\s+checked', body)

    async def test_domains_page_no_filter_pill(self, client):
        """filter-pill class should not appear in domains page."""
        response = await client.get("/domains")
        assert b"filter-pill" not in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_routes.py::TestDomainsPage -v`
Expected: FAIL — `role="radiogroup"` not found (current template uses buttons)

- [ ] **Step 3: Replace filter buttons with segmented control in domains.html**

Replace lines 25–38 in `src/dashboard/templates/pages/domains.html` with:

```html
    <form method="get" action="/domains" class="flex gap-1">
      <input type="hidden" name="q" value="{{ search or '' }}">
      <fieldset class="segment-group" role="radiogroup" aria-label="Filter by status">
        {% for value, label in [("", "All"), ("active", "Active"), ("archived", "Archived"), ("backoff", "Backoff")] %}
        <label class="segment">
          <input type="radio" name="status" value="{{ value }}"
            {% if status == value %}checked{% endif %}
            hx-get="/partials/domains-table"
            hx-target="#domains-table-container"
            hx-swap="innerHTML"
            hx-trigger="change"
            hx-include="[name='q'],[name='page_size']">
          <span>{{ label }}</span>
        </label>
        {% endfor %}
      </fieldset>
      <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
    </form>
```

Also update the search input's `hx-include` (line 22) to use `[name='status']` — it already does, and radio inputs with `name="status"` will be included correctly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_routes.py::TestDomainsPage -v`
Expected: PASS

- [ ] **Step 5: Rebuild Tailwind**

Run: `bash scripts/build-css.sh`

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/templates/pages/domains.html tests/dashboard/test_routes.py src/dashboard/static/css/output.css
git commit -m "#44 feat: domains list — replace filter-pill with segmented control"
```

---

### Task 4: Template — Watches list segmented control

**Files:**
- Modify: `src/dashboard/templates/pages/watches.html:9-13`
- Modify: `src/dashboard/routes.py:61-70` (pass `is_active` to context for checked state)

- [ ] **Step 1: Write failing test — watches page has radio inputs**

Add to `tests/dashboard/test_routes.py`:

```python
class TestWatchListFilters:
    async def test_watches_page_has_segment_control(self, client):
        response = await client.get("/watches")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="is_active"' in body
        assert b'type="radio"' in body

    async def test_watches_page_no_filter_pill(self, client):
        response = await client.get("/watches")
        assert b"filter-pill" not in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_routes.py::TestWatchListFilters -v`
Expected: FAIL

- [ ] **Step 3: Update watches route to pass is_active to context**

In `src/dashboard/routes.py`, update the `watches_page` function context (line 69) to include `is_active`:

```python
    context = {"request": request, "active_page": "watches", "watches": watches, "is_active": is_active}
```

- [ ] **Step 4: Replace filter buttons with segmented control in watches.html**

Replace lines 9–13 in `src/dashboard/templates/pages/watches.html` with:

```html
<form method="get" action="/watches" class="mb-4">
  <fieldset class="segment-group" role="radiogroup" aria-label="Filter by status">
    {% for value, label in [("", "All"), ("true", "Active"), ("false", "Inactive")] %}
    <label class="segment">
      <input type="radio" name="is_active" value="{{ value }}"
        {% if (is_active is true and value == 'true') or (is_active is false and value == 'false') or (is_active is none and value == '') %}checked{% endif %}
        hx-get="/partials/watch-table"
        hx-target="#watch-table"
        hx-swap="innerHTML"
        hx-trigger="change">
      <span>{{ label }}</span>
    </label>
    {% endfor %}
  </fieldset>
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

Note: The `is_active` query param is a bool (`true`/`false`), while the radio value is a string. FastAPI coerces `"true"`→`True`, `"false"`→`False`, `""`→`None`. The checked logic compares against the typed value passed in context. **Important:** Step 3 (route change) must be done before Step 4 (template change), because the template references `is_active` which is not in the current context — Jinja2 will raise `UndefinedError` if the template is deployed without the route change.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_routes.py::TestWatchListFilters -v`
Expected: PASS

- [ ] **Step 6: Rebuild Tailwind and commit**

```bash
bash scripts/build-css.sh
git add src/dashboard/templates/pages/watches.html src/dashboard/routes.py tests/dashboard/test_routes.py src/dashboard/static/css/output.css
git commit -m "#44 feat: watches list — replace filter-pill with segmented control"
```

---

### Task 5: Template — Domain detail watches segmented control

**Files:**
- Modify: `src/dashboard/templates/pages/domain_detail.html:88-102`

- [ ] **Step 1: Write failing test — domain detail has radio inputs for watches filter**

Add to `tests/dashboard/test_routes.py`:

```python
class TestDomainDetailFilters:
    async def _create_domain(self, client):
        """Create a domain via probe endpoint and return the name."""
        response = await client.post(
            "/api/v1/domains", json={"url": "https://example.com"}
        )
        return response.json()["name"]

    async def test_domain_detail_has_segment_control(self, client):
        name = await self._create_domain(client)
        # Create a watch so the filter section appears
        await client.post(
            "/api/v1/watches",
            json={"name": "Domain Filter Watch", "url": f"https://{name}/page", "content_type": "html"},
        )
        response = await client.get(f"/domains/{name}")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="watch_status"' in body

    async def test_domain_detail_no_filter_pill(self, client):
        name = await self._create_domain(client)
        await client.post(
            "/api/v1/watches",
            json={"name": "Domain Filter Watch 2", "url": f"https://{name}/page2", "content_type": "html"},
        )
        response = await client.get(f"/domains/{name}")
        assert b"filter-pill" not in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_routes.py::TestDomainDetailFilters -v`
Expected: FAIL

- [ ] **Step 3: Replace filter buttons in domain_detail.html**

Replace lines 88–102 in `src/dashboard/templates/pages/domain_detail.html` with:

```html
    <form method="get" action="/domains/{{ domain.name }}" class="flex gap-1">
      <fieldset class="segment-group" role="radiogroup" aria-label="Filter watches by status">
        {% for value, label in [("", "All"), ("active", "Active"), ("inactive", "Inactive")] %}
        <label class="segment">
          <input type="radio" name="watch_status" value="{{ value }}"
            {% if watch_status == value or (not watch_status and value == '') %}checked{% endif %}
            hx-get="/domains/{{ domain.name }}"
            hx-target="#domain-watches"
            hx-select="#domain-watches"
            hx-swap="outerHTML"
            hx-trigger="change"
            hx-include="[name='watch_q']">
          <span>{{ label }}</span>
        </label>
        {% endfor %}
      </fieldset>
      <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
    </form>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_routes.py::TestDomainDetailFilters -v`
Expected: PASS

- [ ] **Step 5: Rebuild Tailwind and commit**

```bash
bash scripts/build-css.sh
git add src/dashboard/templates/pages/domain_detail.html tests/dashboard/test_routes.py src/dashboard/static/css/output.css
git commit -m "#44 feat: domain detail — replace filter-pill with segmented control"
```

---

### Task 6: Template — Audit log chip group

**Files:**
- Modify: `src/dashboard/templates/pages/audit_log.html:6-14`
- Modify: `src/dashboard/routes.py:742-756` (pass `event_type` to context for checked state)

- [ ] **Step 1: Write failing test — audit log has checkbox inputs**

Add to `tests/dashboard/test_routes.py`:

```python
class TestAuditLogFilters:
    async def test_audit_page_has_chip_group(self, client):
        response = await client.get("/audit")
        body = response.content
        assert b'class="chip-group"' in body
        assert b'type="checkbox"' in body
        assert b'name="event_type"' in body

    async def test_audit_page_no_filter_pill(self, client):
        response = await client.get("/audit")
        assert b"filter-pill" not in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_routes.py::TestAuditLogFilters -v`
Expected: FAIL

- [ ] **Step 3: Update audit_log route to pass event_type to context**

In `src/dashboard/routes.py`, update the `audit_log_page` function context (line 752–755) to include `event_type`:

```python
    context = {
        "request": request,
        "active_page": "audit",
        "entries": entries,
        "event_type": event_type,
    }
```

- [ ] **Step 4: Replace filter buttons with chip group in audit_log.html**

Replace lines 6–14 in `src/dashboard/templates/pages/audit_log.html` with:

```html
<form method="get" action="/audit" class="mb-4">
  <fieldset class="chip-group" aria-label="Filter by event type">
    <legend class="sr-only">Event types</legend>
    {% for value, label in [
      ("watch.created", "watch.created"),
      ("watch.updated", "watch.updated"),
      ("check.snapshot_created", "check.snapshot_created"),
      ("check.no_change", "check.no_change"),
      ("check.fetch_failed", "check.fetch_failed"),
      ("notification.dispatched", "notification.dispatched"),
    ] %}
    <label class="chip">
      <input type="checkbox" name="event_type" value="{{ value }}"
        {% if event_type == value %}checked{% endif %}
        hx-get="/partials/audit-table"
        hx-target="#audit-table"
        hx-swap="innerHTML"
        hx-trigger="change"
        hx-include=".chip-group input:checked">
      <span>{{ label }}</span>
    </label>
    {% endfor %}
  </fieldset>
  {% if event_type %}
  <a href="/audit" class="link text-sm inline-flex items-center min-h-[44px]"
     hx-get="/partials/audit-table"
     hx-target="#audit-table"
     hx-swap="innerHTML">Clear filter</a>
  {% endif %}
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

Note: The current backend only supports a single `event_type` filter. The chip group template is ready for multi-select but the backend change is out of scope (per design doc). For now, clicking one chip unchecks the previous via HTMX reload (the partial re-renders with the selected value). A future task can add multi-select to the backend.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_routes.py::TestAuditLogFilters -v`
Expected: PASS

- [ ] **Step 6: Rebuild Tailwind and commit**

```bash
bash scripts/build-css.sh
git add src/dashboard/templates/pages/audit_log.html src/dashboard/routes.py tests/dashboard/test_routes.py src/dashboard/static/css/output.css
git commit -m "#44 feat: audit log — replace filter-pill with chip group"
```

---

### Task 7: CSS cleanup — Remove .filter-pill

**Files:**
- Modify: `src/dashboard/static/css/input.css:62-65`

- [ ] **Step 1: Verify no templates reference filter-pill**

Run: `grep -r "filter-pill" src/dashboard/templates/`
Expected: no matches

- [ ] **Step 2: Remove the .filter-pill block from input.css**

Delete lines 62–65 from `input.css`:

```css
  /* -- Filter pill -- */
  .filter-pill {
    @apply px-3 py-1 text-sm rounded-md bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 min-h-[44px] inline-flex items-center;
  }
```

- [ ] **Step 3: Rebuild Tailwind and commit**

```bash
bash scripts/build-css.sh
git add src/dashboard/static/css/input.css src/dashboard/static/css/output.css
git commit -m "#44 refactor: remove unused .filter-pill CSS class"
```

---

### Task 8: Update STYLE.md

**Files:**
- Modify: `docs/STYLE.md`

- [ ] **Step 1: Write failing test — STYLE.md documents new components**

This is a documentation task, so no automated test. Manual verification: read the updated STYLE.md and confirm it matches the implementation.

- [ ] **Step 2: Replace the Filter pill section in STYLE.md**

Find the "### Filter pill" subsection under "## 8. Components" (around lines 169–174) and replace it with two new subsections:

**Subsection 1: "### Segmented control (single-select filter)"**

Content: describe it as a radio-based control for mutually exclusive filter options. Include an HTML example showing `<form method="get">` wrapping a `<fieldset class="segment-group" role="radiogroup">` with `<label class="segment">` elements containing `<input type="radio">` + `<span>`, plus `<noscript>` submit fallback. Add a state table: Inactive (transparent bg, gray text), Active via `input:checked + span` (`co-purple-600` bg, white text), Hover (light gray bg), Focus-visible (purple outline ring). Note: used in domains list, watches list, domain detail watches.

**Subsection 2: "### Chip group (multi-select filter)"**

Content: describe it as a checkbox-based control for toggling multiple filter options. Include an HTML example showing `<form method="get">` wrapping a `<fieldset class="chip-group">` with `<legend class="sr-only">`, `<label class="chip">` elements containing `<input type="checkbox">` + `<span>`, plus `<noscript>` submit fallback. Add a state table: Inactive (transparent bg, gray text, gray border, fully rounded), Active via `input:checked + span` (light purple tint, purple border, purple text), Hover (light gray bg), Focus-visible (purple outline ring). Note: used in audit log.

Use the implemented templates (domains.html, audit_log.html) as the canonical HTML examples.

- [ ] **Step 3: Update the touch targets section (§7)**

Replace the `.filter-pill` line (line 115) with:

```markdown
- `.segment span`: `min-h-[44px]`
- `.chip span`: `min-h-[44px]`
```

- [ ] **Step 4: Commit**

```bash
git add docs/STYLE.md
git commit -m "#44 docs: update STYLE.md with segmented control and chip group patterns"
```

---

### Task 9: Full regression — run all tests

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: all tests pass

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: clean

- [ ] **Step 3: Verify CSS is up to date**

Run: `bash scripts/check-css.sh`
Expected: clean exit (no stale output.css)
