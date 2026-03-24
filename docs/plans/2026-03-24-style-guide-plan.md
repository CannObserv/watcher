# Style Guide & Full Reskin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `docs/STYLE.md`, update `AGENTS.md`, and reskin the entire dashboard to match the Cannabis Observer brand with dark mode, accessibility, and HTMX advanced patterns.

**Architecture:** Tailwind CSS v4 with `@theme` inline config (no `tailwind.config.js` — v4 uses CSS-based config). Brand colors defined as CSS custom properties in `input.css`. Dark mode via Tailwind `darkMode: 'class'`. Layout refactored from simple flex to sidebar+drawer pattern. BUILD_ID env var for cache-busting. Flash macro system with OOB injection for HTMX.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, Tailwind CSS v4.2.2, HTMX

---

## File Structure

### New files
- `docs/STYLE.md` — authoritative style guide document
- `src/dashboard/static/js/dark-mode.js` — dark mode toggle logic
- `src/dashboard/static/js/htmx-a11y.js` — HTMX aria-busy listeners
- `src/dashboard/static/images/cannabis_observer-icon-square.svg` — brand icon
- `src/dashboard/templates/partials/flash_oob.html` — OOB flash injection partial for HTMX responses
- `tests/dashboard/test_build_id.py` — BUILD_ID integration tests
- `tests/dashboard/test_a11y_attributes.py` — accessibility attribute tests

### Modified files
- `src/dashboard/static/css/input.css` — brand tokens, dark mode, components, a11y utilities
- `src/dashboard/static/js/app.js` — flash dismiss with hover-pause, remove old code
- `src/dashboard/templates/base.html` — full rewrite: sidebar, dark mode, FOUC, skip link, ARIA, footer, BUILD_ID
- `src/dashboard/templates/pages/dashboard.html` — brand colors + dark variants
- `src/dashboard/templates/pages/watches.html` — brand colors + dark variants
- `src/dashboard/templates/pages/watch_detail.html` — brand colors, danger zone, detail grid
- `src/dashboard/templates/pages/watch_form.html` — brand focus rings, dark variants
- `src/dashboard/templates/pages/change_detail.html` — brand colors + dark variants
- `src/dashboard/templates/pages/domains.html` — brand colors + dark variants
- `src/dashboard/templates/pages/system.html` — brand colors + dark variants
- `src/dashboard/templates/pages/audit_log.html` — brand colors + dark variants
- `src/dashboard/templates/pages/404.html` — brand colors + dark variants
- `src/dashboard/templates/partials/stats_cards.html` — brand stat card + dark variants
- `src/dashboard/templates/partials/watch_table.html` — dark variants, a11y
- `src/dashboard/templates/partials/watch_row.html` — brand links, dark variants, remove title attr
- `src/dashboard/templates/partials/recent_changes.html` — brand links, dark variants
- `src/dashboard/templates/partials/system_health.html` — brand colors, dark variants
- `src/dashboard/templates/partials/domains_table.html` — dark variants, remove title attr
- `src/dashboard/templates/partials/watch_changes.html` — brand links, dark variants
- `src/dashboard/templates/partials/diff_view.html` — dark diff colors
- `src/dashboard/templates/partials/chunk_table.html` — dark variants, remove title attr
- `src/dashboard/templates/partials/audit_table.html` — dark variants, remove title attr
- `src/dashboard/templates/partials/flash.html` — replace with flash_macro include, add close button
- `src/dashboard/__init__.py` — BUILD_ID Jinja2 global
- `src/dashboard/routes.py` — `_is_htmx()` helper, flash OOB in mutation responses
- `src/api/routes/health.py` — add `build` field to health endpoint
- `AGENTS.md` — add style conventions section

---

## Task 1: Brand Color Tokens in Tailwind CSS

**Files:**
- Modify: `src/dashboard/static/css/input.css`

This task defines the Cannabis Observer brand colors and design tokens using Tailwind v4's CSS-based configuration. All subsequent tasks depend on these tokens.

- [ ] **Step 1: Add brand color tokens and dark mode config to input.css**

Replace the entire `input.css` with brand tokens defined via `@theme`:

```css
@import "tailwindcss";

@source "../../templates/**/*.html";

@custom-variant dark (&:where(.dark, .dark *));

@theme {
  --color-co-purple-50: #f5f0f8;
  --color-co-purple-100: #ebe1f1;
  --color-co-purple-400: #a78bc4;
  --color-co-purple-600: #6d4488;
  --color-co-purple-700: #5a3870;
  --color-co-purple-800: #472c59;
  --color-co-green: #8cbe69;
}

@layer components {
  /* -- Stat card -- */
  .stat-card {
    @apply bg-white dark:bg-gray-800 rounded-lg shadow p-6 border border-gray-200 dark:border-gray-700;
  }

  /* -- Navigation -- */
  .nav-link {
    @apply flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-900 dark:hover:text-white min-h-[44px];
  }
  .nav-link-active {
    @apply bg-co-purple-50 dark:bg-co-purple-800 text-co-purple-600 dark:text-co-purple-400;
  }

  /* -- Data table -- */
  .data-table {
    @apply min-w-full divide-y divide-gray-200 dark:divide-gray-700;
  }
  .data-table thead th {
    @apply px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider bg-white dark:bg-gray-800 sticky top-0 z-10;
    box-shadow: 0 1px 0 var(--color-gray-200);
  }
  .data-table td {
    @apply px-4 py-3 text-sm text-gray-700 dark:text-gray-300;
  }

  /* -- Buttons -- */
  .btn {
    @apply inline-flex items-center justify-center px-4 py-2 text-sm font-medium rounded-md min-h-[44px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-co-purple-600 dark:focus-visible:outline-co-purple-400;
  }
  .btn-primary {
    @apply bg-co-purple-600 text-white hover:bg-co-purple-700 dark:bg-co-purple-600 dark:hover:bg-co-purple-700;
  }
  .btn-secondary {
    @apply text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700;
  }
  .btn-danger {
    @apply text-white bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-800;
  }
  .btn-danger-outline {
    @apply text-red-600 dark:text-red-400 bg-white dark:bg-gray-800 border border-red-300 dark:border-red-600 hover:bg-red-50 dark:hover:bg-red-900/30;
  }
  .btn-ghost {
    @apply text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700;
  }

  /* -- Filter pill -- */
  .filter-pill {
    @apply px-3 py-1 text-sm rounded-md bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 min-h-[44px] inline-flex items-center;
  }

  /* -- Badge -- */
  .badge {
    @apply inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium;
  }
  .badge-active {
    @apply bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300;
  }
  .badge-inactive {
    @apply bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400;
  }
  .badge-error {
    @apply bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300;
  }
  .badge-warning {
    @apply bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-300;
  }
  .badge-info {
    @apply bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300;
  }

  /* -- Flash messages -- */
  .flash {
    @apply p-4 rounded-md border text-sm;
  }
  .flash-success {
    @apply bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-300 border-green-200 dark:border-green-700;
  }
  .flash-error {
    @apply bg-red-50 dark:bg-red-900/30 text-red-800 dark:text-red-300 border-red-200 dark:border-red-700;
  }
  .flash-info {
    @apply bg-blue-50 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300 border-blue-200 dark:border-blue-700;
  }
  .flash-warning {
    @apply bg-yellow-50 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-300 border-yellow-200 dark:border-yellow-700;
  }

  /* -- Alert banner (persistent, non-dismissible) -- */
  .alert {
    @apply p-4 rounded-md border text-sm;
  }
  .alert-notice {
    @apply bg-blue-50 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300 border-blue-200 dark:border-blue-700;
  }
  .alert-warning {
    @apply bg-yellow-50 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-300 border-yellow-200 dark:border-yellow-700;
  }

  /* -- Danger zone -- */
  .danger-zone {
    @apply flex items-center justify-between p-4 border border-red-200 dark:border-red-800 rounded-md bg-red-50/50 dark:bg-red-900/20;
  }
  .danger-zone__label {
    @apply text-sm font-medium text-red-800 dark:text-red-300;
  }
  .danger-zone__desc {
    @apply text-xs text-red-600 dark:text-red-400 mt-0.5;
  }

  /* -- Detail grid -- */
  .detail-grid {
    @apply grid gap-x-4 gap-y-2 text-sm;
    grid-template-columns: minmax(140px, max-content) 1fr;
  }
  .detail-grid dt {
    @apply text-gray-600 dark:text-gray-400;
  }
  .detail-grid dd {
    @apply text-gray-900 dark:text-gray-100 font-medium;
  }

  /* -- Skip link -- */
  .skip-link {
    @apply sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:px-4 focus:py-2 focus:bg-co-purple-600 focus:text-white focus:rounded-md focus:text-sm;
  }

  /* -- Link -- */
  .link {
    @apply text-co-purple-600 dark:text-co-purple-400 hover:text-co-purple-700 dark:hover:text-co-purple-100 hover:underline;
  }

  /* -- Form input -- */
  .form-input {
    @apply block w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 shadow-sm text-sm p-2 min-h-[44px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-co-purple-600 dark:focus-visible:outline-co-purple-400;
  }
  .form-label {
    @apply block text-sm font-medium text-gray-700 dark:text-gray-300;
  }

  /* -- HTMX loading states -- */
  .htmx-request,
  .htmx-request button,
  .htmx-request input,
  .htmx-request select {
    opacity: 0.6;
    cursor: wait;
    pointer-events: none;
  }
}

/* -- Reduced motion -- */
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}

/* -- Flash animation -- */
@keyframes flash-in {
  from {
    opacity: 0;
    transform: translateY(-0.5rem);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.flash {
  animation: flash-in 0.2s ease;
}
```

- [ ] **Step 2: Rebuild Tailwind CSS**

Run: `./scripts/build-css.sh`
Expected: `output.css` regenerated with new brand tokens and component classes.

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/static/css/input.css src/dashboard/static/css/output.css
git commit -m "#34 feat: add brand color tokens, dark mode, and component classes to Tailwind config"
```

---

## Task 2: BUILD_ID Cache-Busting

**Files:**
- Modify: `src/dashboard/__init__.py`
- Modify: `src/api/routes/health.py`
- Create: `tests/dashboard/test_build_id.py`

- [ ] **Step 1: Write failing test for BUILD_ID in health endpoint**

Create `tests/dashboard/test_build_id.py`:

```python
"""Tests for BUILD_ID cache-busting integration."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_health_includes_build_id(client: AsyncClient):
    """Health endpoint includes build field."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "build" in body


@pytest.mark.anyio
async def test_build_id_in_static_asset_urls(client: AsyncClient):
    """Static asset URLs include ?v=BUILD_ID query parameter."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "?v=" in resp.text


@pytest.mark.anyio
async def test_build_id_defaults_to_dev(client: AsyncClient):
    """BUILD_ID defaults to 'dev' when env var is not set."""
    resp = await client.get("/")
    assert resp.status_code == 200
    # BUILD_ID defaults to "dev" at import time when env var unset
    assert "?v=dev" in resp.text
```

Note: `BUILD_ID` is read once at module import time (`os.environ.get("BUILD_ID", "dev")`). Do NOT use `monkeypatch.setenv` — it won't affect the already-evaluated module-level constant. The tests verify the default value (`"dev"`) and the presence of the `?v=` pattern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_build_id.py -v`
Expected: FAIL — no `?v=` in output, no `build` in health response.

- [ ] **Step 3: Add BUILD_ID to Jinja2 globals**

Modify `src/dashboard/__init__.py`:

```python
"""Dashboard — server-rendered UI for watcher."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

BUILD_ID = os.environ.get("BUILD_ID", "dev")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["build_id"] = BUILD_ID


def register_dashboard(app: FastAPI) -> None:
    """Mount static files and include dashboard routes."""
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from src.dashboard.routes import router

    app.include_router(router)
```

- [ ] **Step 4: Add build field to health endpoint**

Modify `src/api/routes/health.py` — update the `health()` function:

```python
import os

# ... existing imports ...

BUILD_ID = os.environ.get("BUILD_ID", "dev")


@router.get("/health")
async def health() -> dict:
    """Liveness probe — confirms the app process is running. No DB call."""
    return {"status": "ok", "build": BUILD_ID}
```

- [ ] **Step 5: Run health endpoint test to verify it passes**

Run: `uv run pytest tests/dashboard/test_build_id.py::test_health_includes_build_id -v`
Expected: PASS — health endpoint now returns `build` field.

The template tests (`test_build_id_in_static_asset_urls`, `test_build_id_defaults_to_dev`) will fail until Task 3 rewrites `base.html` with `?v={{ build_id }}`. That's expected — they go green in Task 3.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/__init__.py src/api/routes/health.py tests/dashboard/test_build_id.py
git commit -m "#34 feat: add BUILD_ID cache-busting and health endpoint build field"
```

---

## Task 3: Base Layout Rewrite

**Files:**
- Modify: `src/dashboard/templates/base.html`
- Create: `src/dashboard/static/js/dark-mode.js`
- Create: `src/dashboard/static/js/htmx-a11y.js`
- Create: `src/dashboard/static/images/cannabis_observer-icon-square.svg`

This is the biggest single task — it rewrites the entire layout shell. All pages extend `base.html`, so this changes everything at once.

- [ ] **Step 1: Create the brand icon SVG**

Create `src/dashboard/static/images/cannabis_observer-icon-square.svg`. This is the Cannabis Observer brand icon — a purple magnifying glass on transparent background:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 510 510">
  <defs>
    <radialGradient id="g" cx="50%" cy="40%" r="50%">
      <stop offset="0%" stop-color="#8cbe69"/>
      <stop offset="100%" stop-color="#6d4488"/>
    </radialGradient>
  </defs>
  <circle cx="220" cy="220" r="160" fill="none" stroke="url(#g)" stroke-width="48"/>
  <line x1="340" y1="340" x2="470" y2="470" stroke="#6d4488" stroke-width="48" stroke-linecap="round"/>
</svg>
```

- [ ] **Step 2: Create dark-mode.js**

Create `src/dashboard/static/js/dark-mode.js`:

```javascript
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
  });

  update();
})();
```

- [ ] **Step 3: Create htmx-a11y.js**

Create `src/dashboard/static/js/htmx-a11y.js`:

```javascript
/**
 * HTMX accessibility helpers.
 * - Sets aria-busy="true" on swap targets during requests.
 * - Removes aria-busy after swap settles.
 */
document.addEventListener("htmx:beforeRequest", function (evt) {
  var target = evt.detail.target;
  if (target) target.setAttribute("aria-busy", "true");
});

document.addEventListener("htmx:afterSettle", function (evt) {
  var target = evt.detail.target;
  if (target) target.removeAttribute("aria-busy");
});
```

- [ ] **Step 4: Rewrite base.html**

Replace `src/dashboard/templates/base.html` with:

```html
<!DOCTYPE html>
<html lang="en" dir="ltr" class="h-full">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}watcher{% endblock %}</title>
  <script>
    (function(){
      var k='watcher-color-scheme',s=localStorage.getItem(k),
          d=window.matchMedia('(prefers-color-scheme: dark)').matches;
      if(s==='dark'||(s===null&&d)){document.documentElement.classList.add('dark');}
    })();
  </script>
  <link rel="stylesheet" href="/static/css/output.css?v={{ build_id }}">
  <link rel="icon" href="/static/images/cannabis_observer-icon-square.svg?v={{ build_id }}" type="image/svg+xml">
  <noscript><style>@media(prefers-color-scheme:dark){html:not(.light){color-scheme:dark}}</style></noscript>
</head>
<body class="h-full bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100">
  <a class="skip-link" href="#main-content">Skip to main content</a>

  <div class="flex h-full">
    <!-- Sidebar (desktop) -->
    <nav class="hidden md:flex w-60 flex-col bg-white dark:bg-gray-800 shadow-sm border-r border-gray-200 dark:border-gray-700" aria-label="Main navigation">
      <div class="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2">
        <img src="/static/images/cannabis_observer-icon-square.svg?v={{ build_id }}" alt="" width="28" height="28" aria-hidden="true">
        <span class="text-lg font-bold text-gray-900 dark:text-white">watcher</span>
      </div>
      <div class="flex-1 p-4 space-y-1">
        <a href="/" class="nav-link {% if active_page == 'dashboard' %}nav-link-active{% endif %}">Dashboard</a>
        <a href="/domains" class="nav-link {% if active_page == 'domains' %}nav-link-active{% endif %}">Domains</a>
        <a href="/watches" class="nav-link {% if active_page == 'watches' %}nav-link-active{% endif %}">Watches</a>
        <a href="/audit" class="nav-link {% if active_page == 'audit' %}nav-link-active{% endif %}">Audit Log</a>
        <a href="/system" class="nav-link {% if active_page == 'system' %}nav-link-active{% endif %}">System</a>
      </div>
      <div class="p-4 border-t border-gray-200 dark:border-gray-700">
        <button class="btn btn-ghost w-full justify-start gap-2" id="theme-toggle" type="button" aria-label="Switch to dark mode">
          <span data-theme-icon aria-hidden="true">&#9789;</span>
          <span class="text-sm">Theme</span>
        </button>
      </div>
    </nav>

    <!-- Mobile topbar + drawer -->
    <div class="flex flex-col flex-1 min-w-0">
      <header class="md:hidden flex items-center justify-between px-4 h-14 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
        <div class="flex items-center gap-2">
          <button id="menu-toggle" class="btn btn-ghost p-2 min-h-[44px] min-w-[44px]" aria-expanded="false" aria-controls="mobile-nav" aria-label="Open navigation menu" type="button">
            <span aria-hidden="true">&#9776;</span>
          </button>
          <span class="text-lg font-bold text-gray-900 dark:text-white">watcher</span>
        </div>
        <button class="btn btn-ghost p-2" id="theme-toggle-mobile" type="button" aria-label="Switch to dark mode">
          <span data-theme-icon aria-hidden="true">&#9789;</span>
        </button>
      </header>

      <!-- Mobile drawer -->
      <div id="mobile-nav" class="fixed inset-0 z-40 hidden md:hidden" role="dialog" aria-modal="true" aria-label="Navigation menu">
        <div id="mobile-backdrop" class="absolute inset-0 bg-black/50"></div>
        <nav class="absolute inset-y-0 left-0 w-65 bg-white dark:bg-gray-800 shadow-lg flex flex-col" aria-label="Main navigation">
          <div class="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
            <div class="flex items-center gap-2">
              <img src="/static/images/cannabis_observer-icon-square.svg?v={{ build_id }}" alt="" width="28" height="28" aria-hidden="true">
              <span class="text-lg font-bold text-gray-900 dark:text-white">watcher</span>
            </div>
            <button id="menu-close" class="btn btn-ghost p-2 min-h-[44px] min-w-[44px]" aria-label="Close navigation menu" type="button">
              <span aria-hidden="true">&times;</span>
            </button>
          </div>
          <div class="flex-1 p-4 space-y-1">
            <a href="/" class="nav-link {% if active_page == 'dashboard' %}nav-link-active{% endif %}">Dashboard</a>
            <a href="/domains" class="nav-link {% if active_page == 'domains' %}nav-link-active{% endif %}">Domains</a>
            <a href="/watches" class="nav-link {% if active_page == 'watches' %}nav-link-active{% endif %}">Watches</a>
            <a href="/audit" class="nav-link {% if active_page == 'audit' %}nav-link-active{% endif %}">Audit Log</a>
            <a href="/system" class="nav-link {% if active_page == 'system' %}nav-link-active{% endif %}">System</a>
          </div>
        </nav>
      </div>

      <!-- Flash region (OOB target) -->
      <div id="flash-region" class="px-4 md:px-8 pt-4" aria-live="polite" aria-atomic="false">
        {% block flash %}{% endblock %}
      </div>

      <!-- Main content -->
      <main id="main-content" class="flex-1 overflow-y-auto px-4 md:px-8 py-6">
        {% block content %}{% endblock %}

        <!-- Footer -->
        <footer class="mt-12 pt-4 border-t border-gray-200 dark:border-gray-700 text-center text-xs text-gray-500 dark:text-gray-400">
          A project of
          <img src="/static/images/cannabis_observer-icon-square.svg?v={{ build_id }}" alt="" width="16" height="16" class="inline-block align-text-bottom" aria-hidden="true">
          Cannabis Observer
          <span aria-hidden="true">🌱🏛️🔍</span>
        </footer>
      </main>
    </div>
  </div>

  <script src="/static/js/htmx.min.js?v={{ build_id }}" defer></script>
  <script src="/static/js/app.js?v={{ build_id }}" defer></script>
  <script src="/static/js/dark-mode.js?v={{ build_id }}" defer></script>
  <script src="/static/js/htmx-a11y.js?v={{ build_id }}" defer></script>
  <script>
    /* Mobile nav toggle */
    (function(){
      var toggle=document.getElementById('menu-toggle');
      var close=document.getElementById('menu-close');
      var drawer=document.getElementById('mobile-nav');
      var backdrop=document.getElementById('mobile-backdrop');
      if(!toggle||!drawer)return;
      function open(){drawer.classList.remove('hidden');toggle.setAttribute('aria-expanded','true');}
      function shut(){drawer.classList.add('hidden');toggle.setAttribute('aria-expanded','false');toggle.focus();}
      toggle.addEventListener('click',open);
      if(close)close.addEventListener('click',shut);
      if(backdrop)backdrop.addEventListener('click',shut);
      document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!drawer.classList.contains('hidden'))shut();});
    })();
    /* Wire up mobile theme toggle to main toggle */
    (function(){
      var main=document.getElementById('theme-toggle');
      var mobile=document.getElementById('theme-toggle-mobile');
      if(main&&mobile)mobile.addEventListener('click',function(){main.click();
        var icon=mobile.querySelector('[data-theme-icon]');
        var mainIcon=main.querySelector('[data-theme-icon]');
        if(icon&&mainIcon)icon.textContent=mainIcon.textContent;
        mobile.setAttribute('aria-label',main.getAttribute('aria-label'));
      });
    })();
  </script>
</body>
</html>
```

- [ ] **Step 5: Rebuild Tailwind CSS**

Run: `./scripts/build-css.sh`

- [ ] **Step 6: Run existing tests and BUILD_ID template tests**

Run: `uv run pytest tests/dashboard/test_routes.py tests/dashboard/test_build_id.py -v`
Expected: All pass — routes still return 200, and `?v=dev` now appears in rendered HTML.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/base.html src/dashboard/static/js/dark-mode.js src/dashboard/static/js/htmx-a11y.js src/dashboard/static/images/cannabis_observer-icon-square.svg src/dashboard/static/css/output.css
git commit -m "#34 feat: rewrite base layout with sidebar, dark mode, skip link, and ARIA landmarks"
```

---

## Task 4: Flash Message System

**Files:**
- Modify: `src/dashboard/templates/partials/flash.html`
- Modify: `src/dashboard/static/js/app.js`

- [ ] **Step 1: Rewrite flash.html with close button and OOB support**

Replace `src/dashboard/templates/partials/flash.html`:

```html
{# Inline flash — used in page templates via {% include %} #}
{% if flash %}
<div class="flash flash-{{ flash.type or 'info' }} flex items-center justify-between mb-4" data-auto-dismiss role="alert">
  <span>{{ flash.message }}</span>
  <button type="button" class="ms-4 text-current opacity-60 hover:opacity-100" aria-label="Dismiss" onclick="this.parentElement.remove()">
    <span aria-hidden="true">&times;</span>
  </button>
</div>
{% endif %}

{# OOB flash macro — call from HTMX partial responses:
   {% include "partials/flash_oob.html" with context %}
   Set flash_oob_level and flash_oob_message before including. #}
```

- [ ] **Step 2: Create flash_oob.html partial for HTMX OOB injection**

Create `src/dashboard/templates/partials/flash_oob.html`:

```html
<div id="flash-region" hx-swap-oob="beforeend">
  <div class="flash flash-{{ flash_oob_level or 'info' }} flex items-center justify-between mb-4" data-auto-dismiss role="alert">
    <span>{{ flash_oob_message }}</span>
    <button type="button" class="ms-4 text-current opacity-60 hover:opacity-100" aria-label="Dismiss" onclick="this.parentElement.remove()">
      <span aria-hidden="true">&times;</span>
    </button>
  </div>
</div>
```

- [ ] **Step 3: Rewrite app.js with hover-pause flash dismiss**

Replace `src/dashboard/static/js/app.js`:

```javascript
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
```

- [ ] **Step 4: Rebuild CSS and commit**

```bash
./scripts/build-css.sh
git add src/dashboard/templates/partials/flash.html src/dashboard/templates/partials/flash_oob.html src/dashboard/static/js/app.js src/dashboard/static/css/output.css
git commit -m "#34 feat: flash message system with close button, hover pause, and OOB injection"
```

---

## Task 5: _is_htmx Helper and Route Updates

**Files:**
- Modify: `src/dashboard/routes.py`

- [ ] **Step 1: Add _is_htmx helper to routes.py**

Add after the imports in `src/dashboard/routes.py`:

```python
def _is_htmx(request: Request) -> bool:
    """Check if request is HTMX (but not boosted navigation)."""
    return bool(
        request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")
    )
```

- [ ] **Step 2: Add OOB flash to deactivate response**

In the `watch_deactivate` route, after the commit, include OOB flash in the HTMX response. Update the return for the row-swap case:

```python
    # After session.commit() and session.refresh(watch):
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "watch-status":
        html = '<dt class="text-sm text-gray-600 dark:text-gray-400">Status</dt>'
        html += '<dd class="text-sm font-medium text-gray-500 dark:text-gray-400">Inactive</dd>'
        return HTMLResponse(content=html)
    return templates.TemplateResponse(
        "partials/watch_row.html", {"request": request, "watch": watch}
    )
```

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/routes.py
git commit -m "#34 feat: add _is_htmx helper and dark mode classes to HTMX responses"
```

---

## Task 6: Reskin Dashboard Home Page

**Files:**
- Modify: `src/dashboard/templates/pages/dashboard.html`
- Modify: `src/dashboard/templates/partials/stats_cards.html`
- Modify: `src/dashboard/templates/partials/recent_changes.html`
- Modify: `src/dashboard/templates/partials/system_health.html`

- [ ] **Step 1: Update dashboard.html**

Replace `src/dashboard/templates/pages/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}Dashboard — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">Dashboard</h2>
<div id="stats-cards" hx-get="/partials/stats-cards" hx-trigger="every 30s" hx-swap="innerHTML" aria-live="polite" aria-atomic="false">
  {% include "partials/stats_cards.html" %}
</div>
<div class="mt-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Recent Changes</h3>
  <div id="recent-changes" hx-get="/partials/recent-changes" hx-trigger="every 30s" hx-swap="innerHTML" aria-live="polite" aria-atomic="false">
    {% include "partials/recent_changes.html" %}
  </div>
</div>
<div class="mt-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">System Health</h3>
  <div id="system-health" hx-get="/partials/system-health" hx-trigger="every 10s" hx-swap="innerHTML" aria-live="polite" aria-atomic="false">
    {% include "partials/system_health.html" %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Update stats_cards.html**

Replace `src/dashboard/templates/partials/stats_cards.html`:

```html
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500 dark:text-gray-400">Total Watches</div>
    <div class="mt-1 text-3xl font-bold text-gray-900 dark:text-white">{{ stats.total_watches }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500 dark:text-gray-400">Active Watches</div>
    <div class="mt-1 text-3xl font-bold text-green-600 dark:text-green-400">{{ stats.active_watches }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500 dark:text-gray-400">Changes Today</div>
    <div class="mt-1 text-3xl font-bold text-co-purple-600 dark:text-co-purple-400">{{ stats.changes_today }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500 dark:text-gray-400">Checks Today</div>
    <div class="mt-1 text-3xl font-bold text-gray-700 dark:text-gray-300">{{ stats.checks_today }}</div>
  </div>
</div>
```

- [ ] **Step 3: Update recent_changes.html**

Replace `src/dashboard/templates/partials/recent_changes.html`:

```html
{% if changes %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Watch</th>
        <th>Detected</th>
        <th>Summary</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for change in changes %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td><a href="/watches/{{ change.watch_id }}" class="link">{{ change.watch_name }}</a></td>
        <td class="text-gray-500 dark:text-gray-400">{{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}</td>
        <td><a href="/changes/{{ change.id }}" class="link">{{ change.summary }}</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No changes detected yet.</p>
{% endif %}
```

- [ ] **Step 4: Update system_health.html**

Replace `src/dashboard/templates/partials/system_health.html`:

```html
<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
  <div class="stat-card">
    <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Task Queue</h4>
    <dl class="space-y-2">
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Pending</dt>
        <dd class="text-sm font-medium text-yellow-600 dark:text-yellow-400">{{ queue.todo }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Running</dt>
        <dd class="text-sm font-medium text-blue-600 dark:text-blue-400">{{ queue.doing }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Failed</dt>
        <dd class="text-sm font-medium text-red-600 dark:text-red-400">{{ queue.failed }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Succeeded (today)</dt>
        <dd class="text-sm font-medium text-green-600 dark:text-green-400">{{ queue.succeeded_today }}</dd>
      </div>
    </dl>
  </div>
  <div class="stat-card">
    <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Rate Limiter</h4>
    {% if domains %}
    <dl class="space-y-2">
      {% for domain in domains %}
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400 truncate max-w-[200px]">{{ domain.name }}</dt>
        <dd class="text-sm font-medium {% if domain.in_backoff %}text-orange-600 dark:text-orange-400{% else %}text-gray-600 dark:text-gray-400{% endif %}">
          {{ "%.1f"|format(domain.current_interval) }}s{% if domain.in_backoff %} <span aria-hidden="true">⚠</span><span class="sr-only">in backoff</span>{% endif %}
        </dd>
      </div>
      {% endfor %}
    </dl>
    {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">No domains tracked yet.</p>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 5: Rebuild CSS and run tests**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/test_routes.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/templates/pages/dashboard.html src/dashboard/templates/partials/stats_cards.html src/dashboard/templates/partials/recent_changes.html src/dashboard/templates/partials/system_health.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin dashboard home with brand colors and dark mode"
```

---

## Task 7: Reskin Watches List Page

**Files:**
- Modify: `src/dashboard/templates/pages/watches.html`
- Modify: `src/dashboard/templates/partials/watch_table.html`
- Modify: `src/dashboard/templates/partials/watch_row.html`

- [ ] **Step 1: Update watches.html**

Replace `src/dashboard/templates/pages/watches.html`:

```html
{% extends "base.html" %}
{% block title %}Watches — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Watches</h2>
  <a href="/watches/new" class="btn btn-primary">New Watch</a>
</div>

<div class="flex gap-2 mb-4 flex-wrap">
  <button hx-get="/partials/watch-table" hx-target="#watch-table" class="filter-pill">All</button>
  <button hx-get="/partials/watch-table?is_active=true" hx-target="#watch-table" class="filter-pill">Active</button>
  <button hx-get="/partials/watch-table?is_active=false" hx-target="#watch-table" class="filter-pill">Inactive</button>
</div>

<div id="watch-table" aria-live="polite" aria-atomic="false">
  {% include "partials/watch_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 2: Update watch_table.html**

Replace `src/dashboard/templates/partials/watch_table.html`:

```html
{% if watches %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Name</th>
        <th>URL</th>
        <th>Type</th>
        <th>Status</th>
        <th>Last Checked</th>
        <th><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for watch in watches %}
        {% include "partials/watch_row.html" %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No watches found.</p>
{% endif %}
```

- [ ] **Step 3: Update watch_row.html**

Replace `src/dashboard/templates/partials/watch_row.html`:

```html
<tr id="watch-{{ watch.id }}" class="hover:bg-gray-50 dark:hover:bg-gray-800">
  <td>
    <a href="/watches/{{ watch.id }}" class="link font-medium">{{ watch.name }}</a>
  </td>
  <td>
    <span class="truncate max-w-[300px] inline-block text-gray-500 dark:text-gray-400">{{ watch.url }}</span>
  </td>
  <td>
    <span class="badge
      {% if watch.content_type == 'html' %}badge-info
      {% elif watch.content_type == 'pdf' %}badge-error
      {% else %}badge-active{% endif %}">
      {{ watch.content_type }}
    </span>
  </td>
  <td>
    {% if watch.is_active %}
      <span class="badge badge-active">Active</span>
    {% else %}
      <span class="badge badge-inactive">Inactive</span>
    {% endif %}
  </td>
  <td class="text-gray-500 dark:text-gray-400">
    {% if watch.last_checked_at %}{{ watch.last_checked_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}Never{% endif %}
  </td>
  <td>
    {% if watch.is_active %}
    <button
      hx-post="/watches/{{ watch.id }}/deactivate"
      hx-target="#watch-{{ watch.id }}"
      hx-swap="outerHTML"
      hx-confirm="Deactivate {{ watch.name }}?"
      class="text-xs text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300 min-h-[44px]">
      Deactivate
    </button>
    {% endif %}
  </td>
</tr>
```

- [ ] **Step 4: Rebuild CSS and run tests**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/test_routes.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/templates/pages/watches.html src/dashboard/templates/partials/watch_table.html src/dashboard/templates/partials/watch_row.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin watches list with brand colors, dark mode, and a11y"
```

---

## Task 8: Reskin Watch Detail Page

**Files:**
- Modify: `src/dashboard/templates/pages/watch_detail.html`

- [ ] **Step 1: Update watch_detail.html**

Replace `src/dashboard/templates/pages/watch_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ watch.name }} — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6 flex-wrap gap-4">
  <div>
    <h2 class="text-2xl font-bold text-gray-900 dark:text-white">{{ watch.name }}</h2>
    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ watch.url }}</p>
  </div>
  <div class="flex gap-2">
    <a href="/watches/{{ watch.id }}/edit" class="btn btn-secondary">Edit</a>
    {% if watch.is_active %}
    <button
      hx-post="/watches/{{ watch.id }}/deactivate"
      hx-target="#watch-status"
      hx-swap="innerHTML"
      hx-confirm="Deactivate {{ watch.name }}?"
      class="btn btn-danger-outline">
      Deactivate
    </button>
    {% else %}
    <button
      hx-delete="/watches/{{ watch.id }}"
      hx-target="#delete-error"
      hx-swap="innerHTML"
      hx-confirm="Permanently delete {{ watch.name }}? This cannot be undone."
      class="btn btn-danger">
      Delete
    </button>
    {% endif %}
  </div>
</div>

<div id="delete-error"></div>

<!-- Status and config -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Configuration</h3>
    <dl class="detail-grid" id="watch-status">
      <dt>Content Type</dt>
      <dd>{{ watch.content_type }}</dd>
      <dt>Status</dt>
      <dd class="{% if watch.is_active %}text-green-600 dark:text-green-400{% else %}text-gray-500 dark:text-gray-400{% endif %}">
        {{ "Active" if watch.is_active else "Inactive" }}
      </dd>
      <dt>Check Interval</dt>
      <dd>{{ watch.schedule_config.get('interval', '1d') if watch.schedule_config else '1d' }}</dd>
      <dt>Last Checked</dt>
      <dd>{% if watch.last_checked_at %}{{ watch.last_checked_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}Never{% endif %}</dd>
    </dl>
  </div>

  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Fetch Config</h3>
    {% if watch.fetch_config %}
    <pre class="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 p-3 rounded overflow-x-auto">{{ watch.fetch_config | tojson(indent=2) }}</pre>
    {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">Default configuration</p>
    {% endif %}
  </div>
</div>

<!-- Temporal Profiles (read-only) -->
{% if profiles %}
<div class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Temporal Profiles</h3>
  <div class="space-y-2">
    {% for profile in profiles %}
    <div class="stat-card text-sm">
      <span class="font-medium text-gray-900 dark:text-white">{{ profile.profile_type }}</span>
      {% if profile.reference_date %} — {{ profile.reference_date }}{% endif %}
      {% if profile.date_range_start %} — {{ profile.date_range_start }} to {{ profile.date_range_end }}{% endif %}
      <span class="text-gray-500 dark:text-gray-400 ms-2">({{ profile.post_action }})</span>
      {% if not profile.is_active %}<span class="text-gray-400 dark:text-gray-500 ms-1">[inactive]</span>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- Notification Configs (read-only) -->
{% if notifications %}
<div class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Notifications</h3>
  <div class="space-y-2">
    {% for nc in notifications %}
    <div class="stat-card text-sm">
      <span class="font-medium text-gray-900 dark:text-white">{{ nc.channel }}</span>
      {% if not nc.is_active %}<span class="text-gray-400 dark:text-gray-500 ms-1">[inactive]</span>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- Change history -->
<div>
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Change History</h3>
  <div id="watch-changes" hx-get="/partials/watch-changes/{{ watch.id }}" hx-trigger="every 30s" hx-swap="innerHTML" aria-live="polite" aria-atomic="false">
    {% include "partials/watch_changes.html" %}
  </div>
</div>
{% endblock %}
```

Note: Uses `detail-grid` class for the configuration section. Uses `ms-2` (logical property `margin-inline-start`) instead of `ml-2`.

- [ ] **Step 2: Rebuild CSS and run tests**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/test_routes.py::test_watch_detail_page -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/templates/pages/watch_detail.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin watch detail with detail grid, brand colors, dark mode"
```

---

## Task 9: Reskin Watch Form Page

**Files:**
- Modify: `src/dashboard/templates/pages/watch_form.html`

- [ ] **Step 1: Update watch_form.html**

Replace `src/dashboard/templates/pages/watch_form.html`:

```html
{% extends "base.html" %}
{% block title %}{{ "Edit" if watch else "New" }} Watch — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">{{ "Edit" if watch else "New" }} Watch</h2>

{% include "partials/flash.html" %}

<form method="post" class="max-w-xl space-y-6">
  <div>
    <label for="name" class="form-label">Name</label>
    <input type="text" name="name" id="name" required
      value="{{ watch.name if watch else '' }}"
      class="form-input mt-1">
  </div>

  <div>
    <label for="url" class="form-label">URL</label>
    <input type="url" name="url" id="url" required
      value="{{ watch.url if watch else '' }}"
      class="form-input mt-1">
  </div>

  <div>
    <label for="content_type" class="form-label">Content Type</label>
    <select name="content_type" id="content_type"
      class="form-input mt-1">
      {% for ct in content_types %}
      <option value="{{ ct.value }}" {% if watch and watch.content_type == ct.value %}selected{% endif %}>{{ ct.value | upper }}</option>
      {% endfor %}
    </select>
  </div>

  <div>
    <label for="interval" class="form-label">Check Interval</label>
    <input type="text" name="interval" id="interval" placeholder="1d"
      value="{{ watch.schedule_config.get('interval', '') if watch and watch.schedule_config else '' }}"
      class="form-input mt-1">
    <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Format: 30s, 15m, 6h, 1d</p>
  </div>

  <div class="flex gap-3">
    <button type="submit" class="btn btn-primary">
      {{ "Save Changes" if watch else "Create Watch" }}
    </button>
    <a href="{{ '/watches/' ~ watch.id if watch else '/watches' }}" class="btn btn-secondary">
      Cancel
    </a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 2: Rebuild CSS and run tests**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/test_routes.py -k "form or create or edit" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/templates/pages/watch_form.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin watch form with brand focus rings and dark mode"
```

---

## Task 10: Reskin Change Detail Page

**Files:**
- Modify: `src/dashboard/templates/pages/change_detail.html`
- Modify: `src/dashboard/templates/partials/diff_view.html`
- Modify: `src/dashboard/templates/partials/chunk_table.html`
- Modify: `src/dashboard/templates/partials/watch_changes.html`

- [ ] **Step 1: Update change_detail.html**

Replace `src/dashboard/templates/pages/change_detail.html`:

```html
{% extends "base.html" %}
{% block title %}Change — {{ watch_name }} — watcher{% endblock %}
{% block content %}
<div class="mb-6">
  <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Change Detail</h2>
  <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
    <a href="/watches/{{ watch_id }}" class="link">{{ watch_name }}</a>
    — detected {{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}
  </p>
</div>

<!-- Change metadata -->
<div class="stat-card mb-6">
  <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Change Summary</h3>
  <div class="flex gap-4 flex-wrap">
    {% set meta = change.change_metadata or {} %}
    {% set added = meta.get('added', []) %}
    {% set modified = meta.get('modified', []) %}
    {% set removed = meta.get('removed', []) %}
    {% if added %}
    <div>
      <span class="text-green-600 dark:text-green-400 font-medium">{{ added | length }} added</span>
      <ul class="text-xs text-gray-500 dark:text-gray-400 mt-1">{% for item in added %}<li>{{ item if item is string else item.label }}</li>{% endfor %}</ul>
    </div>
    {% endif %}
    {% if modified %}
    <div>
      <span class="text-yellow-600 dark:text-yellow-400 font-medium">{{ modified | length }} modified</span>
      <ul class="text-xs text-gray-500 dark:text-gray-400 mt-1">{% for item in modified %}<li>{{ item if item is string else item.label }} {% if item is mapping and item.similarity is defined %}({{ "%.0f" | format(item.similarity * 100) }}% similar){% endif %}</li>{% endfor %}</ul>
    </div>
    {% endif %}
    {% if removed %}
    <div>
      <span class="text-red-600 dark:text-red-400 font-medium">{{ removed | length }} removed</span>
      <ul class="text-xs text-gray-500 dark:text-gray-400 mt-1">{% for item in removed %}<li>{{ item if item is string else item.label }}</li>{% endfor %}</ul>
    </div>
    {% endif %}
  </div>
</div>

<!-- Snapshot info -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">Previous Snapshot</h3>
    {% if previous_snapshot %}
    <dl class="detail-grid">
      <dt>Fetched</dt><dd>{{ previous_snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC') }}</dd>
      <dt>Chunks</dt><dd>{{ previous_snapshot.chunk_count }}</dd>
      <dt>Size</dt><dd>{{ previous_snapshot.text_bytes }} bytes</dd>
      <dt>Fetcher</dt><dd>{{ previous_snapshot.fetcher_used }}</dd>
    </dl>
    {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">Not available</p>
    {% endif %}
  </div>
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">Current Snapshot</h3>
    {% if current_snapshot %}
    <dl class="detail-grid">
      <dt>Fetched</dt><dd>{{ current_snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC') }}</dd>
      <dt>Chunks</dt><dd>{{ current_snapshot.chunk_count }}</dd>
      <dt>Size</dt><dd>{{ current_snapshot.text_bytes }} bytes</dd>
      <dt>Fetcher</dt><dd>{{ current_snapshot.fetcher_used }}</dd>
    </dl>
    {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">Not available</p>
    {% endif %}
  </div>
</div>

<!-- Current chunks -->
{% if current_chunks %}
<div class="mb-6">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Current Chunks</h3>
  {% with chunks=current_chunks %}
  {% include "partials/chunk_table.html" %}
  {% endwith %}
</div>
{% endif %}

<!-- Diff view -->
<div class="mb-6">
  <div class="flex items-center gap-4 mb-4 flex-wrap">
    <h3 class="text-lg font-semibold text-gray-900 dark:text-white">Diff</h3>
    <div class="flex gap-1">
      <button
        hx-get="/partials/diff/{{ change.id }}?mode=extracted"
        hx-target="#diff-content"
        hx-swap="innerHTML"
        class="filter-pill">Extracted Text</button>
      <button
        hx-get="/partials/diff/{{ change.id }}?mode=raw"
        hx-target="#diff-content"
        hx-swap="innerHTML"
        class="filter-pill">Raw Content</button>
    </div>
  </div>
  <div id="diff-content" aria-live="polite" aria-atomic="false">
    {% include "partials/diff_view.html" %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Update diff_view.html**

Replace `src/dashboard/templates/partials/diff_view.html`:

```html
{% if diff.has_changes %}
<div class="overflow-x-auto">
  <table class="w-full text-xs font-mono border border-gray-200 dark:border-gray-700">
    <thead>
      <tr>
        <th class="w-1/2 px-3 py-2 text-left bg-red-50 dark:bg-red-900/30 text-red-800 dark:text-red-300 border-r border-gray-200 dark:border-gray-700">Previous</th>
        <th class="w-1/2 px-3 py-2 text-left bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-300">Current</th>
      </tr>
    </thead>
    <tbody>
      {% for tag, prev_line, curr_line in diff.lines %}
      <tr class="{% if tag == 'replace' %}bg-yellow-50 dark:bg-yellow-900/20{% elif tag == 'delete' %}bg-red-50 dark:bg-red-900/20{% elif tag == 'insert' %}bg-green-50 dark:bg-green-900/20{% endif %}">
        <td class="px-3 py-0.5 border-r border-gray-200 dark:border-gray-700 whitespace-pre-wrap {% if tag in ('delete', 'replace') %}text-red-700 dark:text-red-400{% else %}text-gray-600 dark:text-gray-400{% endif %}">{{ prev_line }}</td>
        <td class="px-3 py-0.5 whitespace-pre-wrap {% if tag in ('insert', 'replace') %}text-green-700 dark:text-green-400{% else %}text-gray-600 dark:text-gray-400{% endif %}">{{ curr_line }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No textual differences found.</p>
{% endif %}
```

- [ ] **Step 3: Update chunk_table.html**

Replace `src/dashboard/templates/partials/chunk_table.html`:

```html
{% if chunks %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>#</th>
        <th>Label</th>
        <th>Type</th>
        <th>Characters</th>
        <th>Excerpt</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for chunk in chunks %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td>{{ chunk.chunk_index }}</td>
        <td class="font-medium text-gray-900 dark:text-white">{{ chunk.chunk_label }}</td>
        <td>{{ chunk.chunk_type }}</td>
        <td>{{ chunk.char_count }}</td>
        <td class="text-gray-500 dark:text-gray-400 truncate max-w-[300px]">{{ chunk.excerpt[:100] }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No chunks.</p>
{% endif %}
```

- [ ] **Step 4: Update watch_changes.html**

Replace `src/dashboard/templates/partials/watch_changes.html`:

```html
{% if changes %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Detected</th>
        <th>Summary</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for change in changes %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td class="text-gray-500 dark:text-gray-400">{{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}</td>
        <td>
          <a href="/changes/{{ change.id }}" class="link">{{ change.summary }}</a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No changes detected yet.</p>
{% endif %}
```

- [ ] **Step 5: Rebuild CSS and run tests**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/test_routes.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/templates/pages/change_detail.html src/dashboard/templates/partials/diff_view.html src/dashboard/templates/partials/chunk_table.html src/dashboard/templates/partials/watch_changes.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin change detail, diff, chunks, and change history"
```

---

## Task 11: Reskin Remaining Pages

**Files:**
- Modify: `src/dashboard/templates/pages/domains.html`
- Modify: `src/dashboard/templates/partials/domains_table.html`
- Modify: `src/dashboard/templates/pages/system.html`
- Modify: `src/dashboard/templates/pages/audit_log.html`
- Modify: `src/dashboard/templates/partials/audit_table.html`
- Modify: `src/dashboard/templates/pages/404.html`

- [ ] **Step 1: Update domains.html**

Replace `src/dashboard/templates/pages/domains.html`:

```html
{% extends "base.html" %}
{% block title %}Domains — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">Domains</h2>

<div id="domains-table-container"
     hx-get="/partials/domains-table"
     hx-trigger="every 30s"
     hx-swap="innerHTML"
     aria-live="polite" aria-atomic="false">
  {% include "partials/domains_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 2: Update domains_table.html**

Replace `src/dashboard/templates/partials/domains_table.html`:

```html
{% if domains %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Domain</th>
        <th>Min Interval</th>
        <th>Current Interval</th>
        <th>Decay Window</th>
        <th>Concurrency</th>
        <th>Watches</th>
        <th>Last 429</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for domain in domains %}
      <tr class="{% if domain.in_backoff %}bg-red-50 dark:bg-red-900/20{% endif %} hover:bg-gray-50 dark:hover:bg-gray-800">
        <td class="font-medium text-gray-900 dark:text-white">{{ domain.name }}</td>
        <td>{{ "%.1f"|format(domain.min_interval) }}s</td>
        <td>{{ "%.1f"|format(domain.current_interval) }}s</td>
        <td>{{ "%.0f"|format(domain.decay_window / 60) }}m</td>
        <td>{{ domain.max_concurrency }}</td>
        <td>{{ domain.watch_count }}</td>
        <td>{{ domain.last_request_at.strftime("%Y-%m-%d %H:%M") if domain.last_request_at else "—" }}</td>
        <td>
          {% if domain.in_backoff %}
          <span class="badge badge-warning">Backoff</span>
          {% else %}
          <span class="badge badge-active">Normal</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No domains configured yet.</p>
{% endif %}
```

- [ ] **Step 3: Update system.html**

Replace `src/dashboard/templates/pages/system.html`:

```html
{% extends "base.html" %}
{% block title %}System — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">System</h2>

<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
  <!-- Queue Health -->
  <div class="stat-card" hx-get="/partials/system-health" hx-trigger="every 10s" hx-swap="innerHTML" hx-select=".stat-card:first-child > *" hx-target="this">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Task Queue</h3>
    <dl class="space-y-2">
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Pending</dt>
        <dd class="text-sm font-medium text-yellow-600 dark:text-yellow-400">{{ queue.todo }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Running</dt>
        <dd class="text-sm font-medium text-blue-600 dark:text-blue-400">{{ queue.doing }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Failed</dt>
        <dd class="text-sm font-medium text-red-600 dark:text-red-400">{{ queue.failed }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 dark:text-gray-400">Succeeded (today)</dt>
        <dd class="text-sm font-medium text-green-600 dark:text-green-400">{{ queue.succeeded_today }}</dd>
      </div>
    </dl>
  </div>

  <!-- Rate Limiter -->
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-3">Rate Limiter</h3>
    {% if domains %}
    <table class="data-table">
      <thead>
        <tr>
          <th>Domain</th>
          <th>Interval</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
        {% for domain in domains %}
        <tr>
          <td class="text-gray-900 dark:text-white">{{ domain.name }}</td>
          <td>{{ "%.1f" | format(domain.current_interval) }}s</td>
          <td>
            {% if domain.in_backoff %}
            <span class="badge badge-warning">Backoff</span>
            {% else %}
            <span class="badge badge-active">Normal</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-gray-500 dark:text-gray-400 text-sm">No domains tracked yet.</p>
    {% endif %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 4: Update audit_log.html**

Replace `src/dashboard/templates/pages/audit_log.html`:

```html
{% extends "base.html" %}
{% block title %}Audit Log — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">Audit Log</h2>

<div class="flex gap-2 mb-4 flex-wrap">
  <button hx-get="/partials/audit-table" hx-target="#audit-table" class="filter-pill">All</button>
  <button hx-get="/partials/audit-table?event_type=watch.created" hx-target="#audit-table" class="filter-pill">watch.created</button>
  <button hx-get="/partials/audit-table?event_type=watch.updated" hx-target="#audit-table" class="filter-pill">watch.updated</button>
  <button hx-get="/partials/audit-table?event_type=check.snapshot_created" hx-target="#audit-table" class="filter-pill">check.snapshot_created</button>
  <button hx-get="/partials/audit-table?event_type=check.no_change" hx-target="#audit-table" class="filter-pill">check.no_change</button>
  <button hx-get="/partials/audit-table?event_type=check.fetch_failed" hx-target="#audit-table" class="filter-pill">check.fetch_failed</button>
  <button hx-get="/partials/audit-table?event_type=notification.dispatched" hx-target="#audit-table" class="filter-pill">notification.dispatched</button>
</div>

<div id="audit-table" aria-live="polite" aria-atomic="false">
  {% include "partials/audit_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 5: Update audit_table.html**

Replace `src/dashboard/templates/partials/audit_table.html`:

```html
{% if entries %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Time</th>
        <th>Event</th>
        <th>Watch</th>
        <th>Details</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for entry in entries %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td class="text-gray-500 dark:text-gray-400 whitespace-nowrap">{{ entry.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') }}</td>
        <td>
          <span class="badge
            {% if 'error' in entry.event_type or 'failed' in entry.event_type %}badge-error
            {% elif 'created' in entry.event_type %}badge-active
            {% elif 'change' in entry.event_type %}badge-info
            {% else %}badge-inactive{% endif %}">
            {{ entry.event_type }}
          </span>
        </td>
        <td>
          {% if entry.watch_id %}
          <a href="/watches/{{ entry.watch_id }}" class="link text-sm">{{ entry.watch_id | string | truncate(12, True, '…') }}</a>
          {% else %}—{% endif %}
        </td>
        <td class="text-xs text-gray-500 dark:text-gray-400 max-w-[300px] truncate">
          {{ entry.payload | tojson | truncate(80, True, '…') }}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No audit entries found.</p>
{% endif %}
```

- [ ] **Step 6: Update 404.html**

Replace `src/dashboard/templates/pages/404.html`:

```html
{% extends "base.html" %}
{% block title %}Not Found — watcher{% endblock %}
{% block content %}
<div class="flex flex-col items-center justify-center py-24 text-center">
  <p class="text-6xl font-bold text-gray-300 dark:text-gray-600 mb-4">404</p>
  <h2 class="text-2xl font-semibold text-gray-800 dark:text-gray-200 mb-2">Not Found</h2>
  <p class="text-gray-500 dark:text-gray-400 mb-8">The page or resource you requested does not exist.</p>
  <a href="/watches" class="btn btn-primary">Back to Watches</a>
</div>
{% endblock %}
```

- [ ] **Step 7: Rebuild CSS and run full test suite**

```bash
./scripts/build-css.sh
```

Run: `uv run pytest tests/dashboard/ -v`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/templates/pages/domains.html src/dashboard/templates/partials/domains_table.html src/dashboard/templates/pages/system.html src/dashboard/templates/pages/audit_log.html src/dashboard/templates/partials/audit_table.html src/dashboard/templates/pages/404.html src/dashboard/static/css/output.css
git commit -m "#34 feat: reskin domains, system, audit log, and 404 pages"
```

---

## Task 12: Accessibility Tests

**Files:**
- Create: `tests/dashboard/test_a11y_attributes.py`

- [ ] **Step 1: Write accessibility attribute tests**

Create `tests/dashboard/test_a11y_attributes.py`:

```python
"""Tests for accessibility attributes in dashboard templates."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_skip_link_present(client: AsyncClient):
    """Dashboard pages include a skip-to-content link."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'href="#main-content"' in resp.text
    assert "Skip to main content" in resp.text


@pytest.mark.anyio
async def test_main_landmark_present(client: AsyncClient):
    """Dashboard pages have a main landmark with correct id."""
    resp = await client.get("/")
    assert 'id="main-content"' in resp.text


@pytest.mark.anyio
async def test_nav_landmark_has_aria_label(client: AsyncClient):
    """Navigation landmark has an aria-label."""
    resp = await client.get("/")
    assert 'aria-label="Main navigation"' in resp.text


@pytest.mark.anyio
async def test_html_lang_and_dir(client: AsyncClient):
    """HTML element has lang and dir attributes."""
    resp = await client.get("/")
    assert 'lang="en"' in resp.text
    assert 'dir="ltr"' in resp.text


@pytest.mark.anyio
async def test_htmx_swap_targets_have_live_region(client: AsyncClient):
    """HTMX swap targets on dashboard have aria-live attributes."""
    resp = await client.get("/")
    assert 'aria-live="polite"' in resp.text


@pytest.mark.anyio
async def test_decorative_emoji_hidden(client: AsyncClient):
    """Decorative emojis are wrapped in aria-hidden."""
    resp = await client.get("/")
    # Footer emoji triad should be hidden
    assert 'aria-hidden="true">🌱🏛️🔍</span>' in resp.text


@pytest.mark.anyio
async def test_dark_mode_toggle_has_aria_label(client: AsyncClient):
    """Dark mode toggle button has an aria-label."""
    resp = await client.get("/")
    assert 'id="theme-toggle"' in resp.text
    assert "aria-label" in resp.text
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_a11y_attributes.py -v`
Expected: All PASS (these test the base layout we already created in Task 3).

- [ ] **Step 3: Commit**

```bash
git add tests/dashboard/test_a11y_attributes.py
git commit -m "#34 test: add accessibility attribute tests for dashboard"
```

---

## Task 13: Write docs/STYLE.md

**Files:**
- Create: `docs/STYLE.md`

- [ ] **Step 1: Write the authoritative style guide**

Create `docs/STYLE.md` with the full style guide document. This codifies all conventions implemented in Tasks 1–12. The document should cover:

1. Brand Assets — icon paths, sizes, footer emoji
2. Color Palette — brand tokens, semantic status colors (never brand-colored)
3. Dark Mode — class-based, localStorage key, FOUC prevention, no-JS fallback
4. CSS Design Token System — Tailwind v4 `@theme` block, naming conventions
5. Layout — sidebar + main grid, mobile drawer, responsive breakpoints
6. Touch Targets — 44px minimum on all interactive elements
7. Components — stat-card, data-table, badge, btn variants, filter-pill, flash, alert, danger-zone, detail-grid, form-input, link, skip-link, pagination (pattern documented, implementation deferred until list views need it), modal (pattern documented, focus trapping deferred to #35)
8. HTMX Patterns — `_is_htmx()`, OOB flash, loading states, live regions, aria-busy, graceful degradation
9. Flash / Notification UX — macros, levels, auto-dismiss, hover pause, XSS prevention
10. Accessibility (WCAG 2.1 AA) — emoji, focus rings, icon buttons, live regions, skip link, reduced motion, muted text minimum, no title attributes
11. Internationalization Groundwork — lang/dir, charset, logical CSS properties, NFC normalization
12. Performance — no CDN, defer scripts, BUILD_ID cache-busting, system font stack, explicit image dimensions
13. Responsive Breakpoints — mobile (<768px), small (<640px), medium (≥768px), large (≥1024px)

Write the full document based on the design doc and all the CSS/templates from earlier tasks. Reference actual file paths and current implementations.

- [ ] **Step 2: Commit**

```bash
git add docs/STYLE.md
git commit -m "#34 docs: add authoritative style guide"
```

---

## Task 14: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add style conventions section to AGENTS.md**

Add a new section after the "Conventions" section in `AGENTS.md`:

```markdown
## Style & UI Conventions

Authoritative reference: `docs/STYLE.md`

**Brand:** Cannabis Observer — `co-purple-600` (#6d4488) primary accent. Never use brand colors for semantic status (green/yellow/red/blue).

**Dark Mode:** Tailwind `dark:` variants on every color utility. Class-based toggle (`<html class="dark">`). localStorage key: `watcher-color-scheme`.

**Accessibility:** WCAG 2.1 AA. Skip link, ARIA landmarks, `focus-visible` rings, 44px touch targets, `aria-live` on HTMX swap targets, reduced motion. Wrap decorative emoji in `<span aria-hidden="true">`. No `title` attributes.

**CSS:** Tailwind v4 with `@theme` in `input.css`. Use component classes (`.btn`, `.badge`, `.stat-card`, `.data-table`, `.form-input`, `.link`, `.filter-pill`, `.detail-grid`). CSS logical properties (`margin-inline-start` not `margin-left`).

**HTMX:** OOB flash via `partials/flash_oob.html`. CSS `.htmx-request` for loading states. `_is_htmx(request)` checks `HX-Request` with `HX-Boosted` guard. All mutation routes provide non-HTMX redirect fallback.

**Performance:** Pre-built Tailwind (no CDN). `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`). `defer` on all non-critical scripts. System font stack.

**Env Vars:**
- `BUILD_ID` — (optional) git SHA for static asset cache-busting; defaults to `"dev"`
```

- [ ] **Step 2: Add BUILD_ID to the secrets/env section**

In the `Secrets` section of `AGENTS.md`, add:

```markdown
- `BUILD_ID` — (optional) git SHA for static asset cache-busting; defaults to `"dev"`
```

- [ ] **Step 3: Update the project layout to include new files**

In the `Project Layout` section, update the `src/dashboard/` entries:

```
src/dashboard/static/    — CSS, JS (vendored HTMX, dark-mode, htmx-a11y), compiled Tailwind
src/dashboard/static/images/ — Brand assets (Cannabis Observer icon)
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "#34 docs: add style and UI conventions to AGENTS.md"
```

---

## Task 15: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass.

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: No errors.

- [ ] **Step 3: Rebuild CSS one final time**

Run: `./scripts/build-css.sh`

- [ ] **Step 4: Visual check — start dev server**

Run: `export $(cat env | xargs) && uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000`

Visit in browser:
- `http://localhost:8000/` — dashboard with brand colors, dark mode toggle works
- `http://localhost:8000/watches` — brand links, filter pills, dark mode
- `http://localhost:8000/system` — system health with dark variants
- Toggle dark mode — all pages should render correctly in both modes
- Resize to mobile width — sidebar should collapse, hamburger menu should work

- [ ] **Step 5: Commit any final CSS output changes**

```bash
git add src/dashboard/static/css/output.css
git commit -m "#34 chore: rebuild Tailwind CSS with final template changes"
```

(Skip this commit if output.css hasn't changed since last commit.)
