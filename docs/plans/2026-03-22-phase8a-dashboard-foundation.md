# Phase 8a: Dashboard Foundation + Home Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Server-rendered dashboard home page with stats cards, recent changes table, and system health — auto-refreshing via HTMX partials.

**Architecture:** Jinja2 templates served by FastAPI routes in `src/dashboard/`. HTMX handles partial refreshes without full page reloads. Tailwind CSS compiled via standalone CLI. Dashboard registers itself into the existing FastAPI app via `register_dashboard(app)`.

**Tech Stack:** Jinja2, HTMX 2.0.8 (vendored), Tailwind CSS (standalone CLI), pre-commit, python-multipart

**Design doc:** `docs/plans/2026-03-21-phase8-dashboard-design.md`
**Issue:** #6

---

## File Structure

```
src/dashboard/
  __init__.py              — register_dashboard(app)
  routes.py                — dashboard page routes
  context.py               — DB query helpers for dashboard data
  static/
    css/
      input.css            — Tailwind source
    js/
      htmx.min.js          — vendored HTMX 2.0.8
      app.js               — custom JS
  templates/
    base.html              — layout with sidebar nav
    partials/
      stats_cards.html     — watch/change/check counts
      recent_changes.html  — last 20 changes table
      system_health.html   — queue + rate limiter status
    pages/
      dashboard.html       — home page assembling partials

scripts/
  build-css.sh             — Tailwind CLI compile wrapper

.pre-commit-config.yaml    — pre-commit hooks (ruff + CSS validation)
```

---

## Task 1: Add dependencies and Tailwind CLI

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/build-css.sh`

- [ ] **Step 1: Add jinja2, python-multipart, pre-commit to dependencies**

```bash
uv add jinja2 python-multipart
uv add --group dev pre-commit
```

- [ ] **Step 2: Download Tailwind standalone CLI**

```bash
mkdir -p scripts
curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
chmod +x tailwindcss-linux-x64
mv tailwindcss-linux-x64 scripts/tailwindcss
```

- [ ] **Step 3: Create build-css.sh**

Create `scripts/build-css.sh`:

```bash
#!/usr/bin/env bash
# Build Tailwind CSS from source to output.
# Usage: bash scripts/build-css.sh [--watch]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TAILWIND="$SCRIPT_DIR/tailwindcss"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"

if [ ! -f "$TAILWIND" ]; then
  echo "Error: Tailwind CLI not found at $TAILWIND"
  echo "Download it: curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 && chmod +x tailwindcss-linux-x64 && mv tailwindcss-linux-x64 scripts/tailwindcss"
  exit 1
fi

if [ "${1:-}" = "--watch" ]; then
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --watch
else
  "$TAILWIND" -i "$INPUT" -o "$OUTPUT" --minify
fi
```

- [ ] **Step 4: Add to .gitignore**

Append to `.gitignore`:
```
# Tailwind
src/dashboard/static/css/output.css
scripts/tailwindcss
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock scripts/build-css.sh .gitignore
git commit -m "#6 chore: add jinja2, python-multipart, pre-commit, Tailwind CLI setup"
```

---

## Task 2: Pre-commit CSS validation hook

**Files:**
- Create: `.pre-commit-config.yaml`
- Create: `scripts/check-css.sh`

- [ ] **Step 1: Create check-css.sh**

Create `scripts/check-css.sh`:

```bash
#!/usr/bin/env bash
# Pre-commit hook: verify output.css is up to date with templates and input.css.
# Exits non-zero if CSS needs recompilation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TAILWIND="$SCRIPT_DIR/tailwindcss"
INPUT="$ROOT_DIR/src/dashboard/static/css/input.css"
OUTPUT="$ROOT_DIR/src/dashboard/static/css/output.css"

if [ ! -f "$TAILWIND" ]; then
  echo "⚠ Tailwind CLI not found — skipping CSS check"
  exit 0
fi

if [ ! -f "$INPUT" ]; then
  exit 0  # No dashboard CSS yet
fi

# Build to a temp file and compare
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

"$TAILWIND" -i "$INPUT" -o "$TMPFILE" --minify 2>/dev/null

if [ ! -f "$OUTPUT" ]; then
  echo "❌ output.css missing. Run: bash scripts/build-css.sh"
  exit 1
fi

if ! diff -q "$OUTPUT" "$TMPFILE" > /dev/null 2>&1; then
  echo "❌ output.css is stale. Run: bash scripts/build-css.sh"
  exit 1
fi
```

- [ ] **Step 2: Create .pre-commit-config.yaml**

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff lint
        entry: uv run ruff check .
        language: system
        types: [python]
        pass_filenames: false

      - id: ruff-format-check
        name: ruff format check
        entry: uv run ruff format --check .
        language: system
        types: [python]
        pass_filenames: false

      - id: tailwind-css
        name: tailwind CSS up to date
        entry: bash scripts/check-css.sh
        language: system
        files: '(\.html|input\.css)$'
        pass_filenames: false
```

- [ ] **Step 3: Install pre-commit hooks**

```bash
uv run pre-commit install
```

- [ ] **Step 4: Commit**

```bash
git add .pre-commit-config.yaml scripts/check-css.sh
git commit -m "#6 chore: add pre-commit hooks (ruff, Tailwind CSS validation)"
```

---

## Task 3: Vendor HTMX and create static file structure

**Files:**
- Create: `src/dashboard/__init__.py`
- Create: `src/dashboard/static/js/htmx.min.js`
- Create: `src/dashboard/static/js/app.js`
- Create: `src/dashboard/static/css/input.css`

- [ ] **Step 1: Create directory structure and vendor HTMX**

```bash
mkdir -p src/dashboard/static/{css,js}
mkdir -p src/dashboard/templates/{pages,partials}
curl -sL https://unpkg.com/htmx.org@2.0.8/dist/htmx.min.js -o src/dashboard/static/js/htmx.min.js
```

- [ ] **Step 2: Create input.css**

Create `src/dashboard/static/css/input.css`:

```css
@import "tailwindcss";

@source "../../templates/**/*.html";

/* Custom dashboard styles */
@layer components {
  .stat-card {
    @apply bg-white rounded-lg shadow p-6;
  }
  .nav-link {
    @apply flex items-center px-4 py-2 text-sm font-medium text-gray-600 rounded-md hover:bg-gray-100 hover:text-gray-900;
  }
  .nav-link-active {
    @apply bg-gray-100 text-gray-900;
  }
  .data-table {
    @apply min-w-full divide-y divide-gray-200;
  }
  .data-table th {
    @apply px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider;
  }
  .data-table td {
    @apply px-4 py-3 text-sm text-gray-700;
  }
}
```

- [ ] **Step 3: Create app.js**

Create `src/dashboard/static/js/app.js`:

```javascript
/**
 * watcher dashboard — custom JS
 *
 * Handles: diff view toggling, flash message dismissal.
 * HTMX handles all partial loading and form submission.
 */

document.addEventListener("DOMContentLoaded", function () {
  // Auto-dismiss flash messages after 5 seconds
  document.querySelectorAll("[data-auto-dismiss]").forEach(function (el) {
    setTimeout(function () {
      el.remove();
    }, 5000);
  });
});

/**
 * Toggle between extracted text and raw content diff views.
 * Used on the change detail page (Phase 8c).
 */
function toggleDiffView(mode) {
  document.querySelectorAll("[data-diff-view]").forEach(function (el) {
    el.classList.toggle("hidden", el.dataset.diffView !== mode);
  });
  document.querySelectorAll("[data-diff-toggle]").forEach(function (btn) {
    btn.classList.toggle("bg-gray-200", btn.dataset.diffToggle === mode);
    btn.classList.toggle("bg-white", btn.dataset.diffToggle !== mode);
  });
}
```

- [ ] **Step 4: Build CSS**

```bash
bash scripts/build-css.sh
```

- [ ] **Step 5: Create empty `src/dashboard/__init__.py`**

```python
"""Dashboard — server-rendered UI for watcher."""
```

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/ scripts/build-css.sh
git commit -m "#6 feat: add static file structure, vendor HTMX 2.0.8, Tailwind input"
```

---

## Task 4: Base template and dashboard registration

**Files:**
- Create: `src/dashboard/templates/base.html`
- Modify: `src/dashboard/__init__.py`
- Modify: `src/api/main.py`
- Create: `tests/dashboard/__init__.py`
- Create: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing test**

Create `tests/dashboard/__init__.py` (empty) and `tests/dashboard/test_routes.py`:

```python
"""Integration tests for dashboard routes."""

import pytest

pytestmark = pytest.mark.integration


class TestDashboardHome:
    async def test_home_returns_200(self, client):
        response = await client.get("/")
        assert response.status_code == 200

    async def test_home_contains_title(self, client):
        response = await client.get("/")
        assert b"watcher" in response.content.lower()

    async def test_home_contains_nav(self, client):
        response = await client.get("/")
        assert b"Dashboard" in response.content
        assert b"Watches" in response.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/dashboard/test_routes.py -v -m integration`
Expected: FAIL — 404 (no route registered)

- [ ] **Step 3: Create base.html**

Create `src/dashboard/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en" class="h-full bg-gray-50">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}watcher{% endblock %}</title>
  <link rel="stylesheet" href="/static/css/output.css">
  <script src="/static/js/htmx.min.js"></script>
  <script src="/static/js/app.js"></script>
</head>
<body class="h-full">
  <div class="flex h-full">
    <!-- Sidebar -->
    <nav class="w-64 bg-white shadow-sm border-r border-gray-200 flex flex-col">
      <div class="p-4 border-b border-gray-200">
        <h1 class="text-lg font-bold text-gray-900">watcher</h1>
      </div>
      <div class="flex-1 p-4 space-y-1">
        <a href="/" class="nav-link {% if active_page == 'dashboard' %}nav-link-active{% endif %}">
          Dashboard
        </a>
        <a href="/watches" class="nav-link {% if active_page == 'watches' %}nav-link-active{% endif %}">
          Watches
        </a>
        <a href="/audit" class="nav-link {% if active_page == 'audit' %}nav-link-active{% endif %}">
          Audit Log
        </a>
        <a href="/system" class="nav-link {% if active_page == 'system' %}nav-link-active{% endif %}">
          System
        </a>
      </div>
    </nav>

    <!-- Main content -->
    <main class="flex-1 overflow-y-auto p-8">
      {% block content %}{% endblock %}
    </main>
  </div>
</body>
</html>
```

- [ ] **Step 4: Create dashboard page template**

Create `src/dashboard/templates/pages/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}Dashboard — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 mb-6">Dashboard</h2>

<div id="stats-cards" hx-get="/partials/stats-cards" hx-trigger="every 30s" hx-swap="innerHTML">
  {% include "partials/stats_cards.html" %}
</div>

<div class="mt-8">
  <h3 class="text-lg font-semibold text-gray-900 mb-4">Recent Changes</h3>
  <div id="recent-changes" hx-get="/partials/recent-changes" hx-trigger="every 30s" hx-swap="innerHTML">
    {% include "partials/recent_changes.html" %}
  </div>
</div>

<div class="mt-8">
  <h3 class="text-lg font-semibold text-gray-900 mb-4">System Health</h3>
  <div id="system-health" hx-get="/partials/system-health" hx-trigger="every 10s" hx-swap="innerHTML">
    {% include "partials/system_health.html" %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 5: Create placeholder partials**

Create `src/dashboard/templates/partials/stats_cards.html`:

```html
<div class="grid grid-cols-1 md:grid-cols-4 gap-4">
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500">Total Watches</div>
    <div class="mt-1 text-3xl font-bold text-gray-900">{{ stats.total_watches }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500">Active Watches</div>
    <div class="mt-1 text-3xl font-bold text-green-600">{{ stats.active_watches }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500">Changes Today</div>
    <div class="mt-1 text-3xl font-bold text-blue-600">{{ stats.changes_today }}</div>
  </div>
  <div class="stat-card">
    <div class="text-sm font-medium text-gray-500">Checks Today</div>
    <div class="mt-1 text-3xl font-bold text-gray-700">{{ stats.checks_today }}</div>
  </div>
</div>
```

Create `src/dashboard/templates/partials/recent_changes.html`:

```html
{% if changes %}
<table class="data-table">
  <thead>
    <tr>
      <th>Watch</th>
      <th>Detected</th>
      <th>Summary</th>
    </tr>
  </thead>
  <tbody class="divide-y divide-gray-100">
    {% for change in changes %}
    <tr>
      <td>
        <a href="/watches/{{ change.watch_id }}" class="text-blue-600 hover:underline">
          {{ change.watch_name }}
        </a>
      </td>
      <td class="text-gray-500">{{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}</td>
      <td>
        <a href="/changes/{{ change.id }}" class="text-blue-600 hover:underline">
          {{ change.summary }}
        </a>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="text-gray-500 text-sm">No changes detected yet.</p>
{% endif %}
```

Create `src/dashboard/templates/partials/system_health.html`:

```html
<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
  <!-- Queue Health -->
  <div class="stat-card">
    <h4 class="text-sm font-medium text-gray-500 mb-3">Task Queue</h4>
    <dl class="space-y-2">
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Pending</dt>
        <dd class="text-sm font-medium text-yellow-600">{{ queue.todo }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Running</dt>
        <dd class="text-sm font-medium text-blue-600">{{ queue.doing }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Failed</dt>
        <dd class="text-sm font-medium text-red-600">{{ queue.failed }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Succeeded (today)</dt>
        <dd class="text-sm font-medium text-green-600">{{ queue.succeeded_today }}</dd>
      </div>
    </dl>
  </div>

  <!-- Rate Limiter -->
  <div class="stat-card">
    <h4 class="text-sm font-medium text-gray-500 mb-3">Rate Limiter</h4>
    {% if domains %}
    <dl class="space-y-2">
      {% for domain in domains %}
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600 truncate max-w-[200px]" title="{{ domain.name }}">{{ domain.name }}</dt>
        <dd class="text-sm font-medium {% if domain.in_backoff %}text-orange-600{% else %}text-gray-600{% endif %}">
          {{ "%.1f"|format(domain.interval) }}s{% if domain.in_backoff %} ⚠{% endif %}
        </dd>
      </div>
      {% endfor %}
    </dl>
    {% else %}
    <p class="text-gray-500 text-sm">No domains tracked yet.</p>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 6: Implement register_dashboard and routes**

Update `src/dashboard/__init__.py`:

```python
"""Dashboard — server-rendered UI for watcher."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def register_dashboard(app: FastAPI) -> None:
    """Mount static files and include dashboard routes."""
    # Only mount static files if the directory exists (output.css is gitignored
    # and may not exist in CI/test environments before a CSS build).
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from src.dashboard.routes import router
    app.include_router(router)
```

Create `src/dashboard/routes.py`:

```python
"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.dashboard import templates
from src.dashboard.context import get_dashboard_stats, get_queue_health, get_rate_limiter_state, get_recent_changes

router = APIRouter(tags=["dashboard"])


@router.get("/")
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard home page with stats, recent changes, and system health."""
    stats = await get_dashboard_stats(session)
    changes = await get_recent_changes(session, limit=20)
    queue = await get_queue_health(session)
    domains = get_rate_limiter_state()

    context = {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "changes": changes,
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/dashboard.html", context)


@router.get("/partials/stats-cards")
async def partial_stats_cards(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: stats cards only."""
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(
        "partials/stats_cards.html", {"request": request, "stats": stats}
    )


@router.get("/partials/recent-changes")
async def partial_recent_changes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: recent changes table."""
    changes = await get_recent_changes(session, limit=20)
    return templates.TemplateResponse(
        "partials/recent_changes.html", {"request": request, "changes": changes}
    )


@router.get("/partials/system-health")
async def partial_system_health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: queue health and rate limiter."""
    queue = await get_queue_health(session)
    domains = get_rate_limiter_state()
    return templates.TemplateResponse(
        "partials/system_health.html",
        {"request": request, "queue": queue, "domains": domains},
    )
```

- [ ] **Step 7: Update main.py to register dashboard**

Add to `src/api/main.py` after existing router includes:

```python
from src.dashboard import register_dashboard
register_dashboard(app)
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/dashboard/test_routes.py -v -m integration`
Expected: PASS (3 tests — 200 status, title, nav links)

- [ ] **Step 9: Commit**

```bash
git add src/dashboard/ src/api/main.py tests/dashboard/
git commit -m "#6 feat: add base template, dashboard routes, register_dashboard"
```

---

## Task 5: Dashboard context queries

**Files:**
- Create: `src/dashboard/context.py`
- Create: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests**

Create `tests/dashboard/test_context.py`:

```python
"""Integration tests for dashboard context queries."""

import pytest
from ulid import ULID

from src.core.models.audit_log import AuditLog
from src.core.models.change import Change
from src.core.models.watch import Watch
from src.dashboard.context import (
    get_dashboard_stats,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
)

pytestmark = pytest.mark.integration


class TestGetDashboardStats:
    async def test_empty_database(self, db_session):
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 0
        assert stats["active_watches"] == 0
        assert stats["changes_today"] == 0
        assert stats["checks_today"] == 0

    async def test_counts_watches(self, db_session):
        db_session.add(Watch(name="W1", url="https://a.com", content_type="html"))
        db_session.add(Watch(name="W2", url="https://b.com", content_type="html", is_active=False))
        await db_session.flush()
        stats = await get_dashboard_stats(db_session)
        assert stats["total_watches"] == 2
        assert stats["active_watches"] == 1


class TestGetRecentChanges:
    async def test_empty(self, db_session):
        changes = await get_recent_changes(db_session)
        assert changes == []

    async def test_returns_changes_with_watch_name(self, db_session):
        watch = Watch(name="Test Watch", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()
        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
            change_metadata={"added": ["Page 1"]},
        )
        db_session.add(change)
        await db_session.flush()
        changes = await get_recent_changes(db_session, limit=10)
        assert len(changes) == 1
        assert changes[0]["watch_name"] == "Test Watch"
        assert changes[0]["id"] is not None


class TestGetQueueHealth:
    async def test_returns_queue_stats(self, db_session):
        queue = await get_queue_health(db_session)
        assert "todo" in queue
        assert "doing" in queue
        assert "failed" in queue
        assert "succeeded_today" in queue


class TestGetRateLimiterState:
    def test_returns_list(self):
        domains = get_rate_limiter_state()
        assert isinstance(domains, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_context.py -v -m integration`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement context.py**

Create `src/dashboard/context.py`:

```python
"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.audit_log import AuditLog
from src.core.models.change import Change
from src.core.models.watch import Watch
from src.core.rate_limiter import DEFAULT_MIN_INTERVAL


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate counts for dashboard stat cards."""
    total = await session.scalar(select(func.count(Watch.id)))
    active = await session.scalar(
        select(func.count(Watch.id)).where(Watch.is_active.is_(True))
    )

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    changes_today = await session.scalar(
        select(func.count(Change.id)).where(Change.detected_at >= today_start)
    )
    checks_today = await session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.event_type.in_(["check.snapshot_created", "check.no_change", "check.fetch_failed"]),
            AuditLog.created_at >= today_start,
        )
    )

    return {
        "total_watches": total or 0,
        "active_watches": active or 0,
        "changes_today": changes_today or 0,
        "checks_today": checks_today or 0,
    }


async def get_recent_changes(
    session: AsyncSession, limit: int = 20
) -> list[dict]:
    """Fetch recent changes with watch names for display."""
    stmt = (
        select(Change, Watch.name)
        .join(Watch, Change.watch_id == Watch.id)
        .order_by(Change.detected_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()

    changes = []
    for change, watch_name in rows:
        meta = change.change_metadata or {}
        added = len(meta.get("added", []))
        modified = len(meta.get("modified", []))
        removed = len(meta.get("removed", []))
        parts = []
        if added:
            parts.append(f"{added} added")
        if modified:
            parts.append(f"{modified} modified")
        if removed:
            parts.append(f"{removed} removed")
        summary = ", ".join(parts) if parts else "change detected"

        changes.append({
            "id": str(change.id),
            "watch_id": str(change.watch_id),
            "watch_name": watch_name,
            "detected_at": change.detected_at,
            "summary": summary,
        })
    return changes


async def get_queue_health(session: AsyncSession) -> dict:
    """Query procrastinate_jobs table for queue status counts."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # procrastinate_jobs uses status enum: todo, doing, succeeded, failed
    try:
        result = await session.execute(
            text("""
                SELECT status, count(*)
                FROM procrastinate_jobs
                GROUP BY status
            """)
        )
        counts = {row[0]: row[1] for row in result.all()}
    except Exception:
        # Table may not exist in test environments
        counts = {}

    # Succeeded today
    try:
        succeeded_today = await session.scalar(
            text("""
                SELECT count(*)
                FROM procrastinate_jobs
                WHERE status = 'succeeded'
                AND scheduled_at >= :today_start
            """),
            {"today_start": today_start},
        )
    except Exception:
        succeeded_today = 0

    return {
        "todo": counts.get("todo", 0),
        "doing": counts.get("doing", 0),
        "failed": counts.get("failed", 0),
        "succeeded_today": succeeded_today or 0,
    }


def get_rate_limiter_state() -> list[dict]:
    """Get current rate limiter domain states.

    Returns list of dicts with domain name, interval, and backoff status.
    Imports lazily to avoid circular imports and event loop issues.
    """
    try:
        from src.workers.tasks import get_rate_limiter
        limiter = get_rate_limiter()
    except Exception:
        return []

    domains = []
    for domain, state in limiter._domains.items():
        domains.append({
            "name": domain,
            "interval": state.min_interval,
            "in_backoff": state.min_interval > DEFAULT_MIN_INTERVAL,
        })
    return sorted(domains, key=lambda d: d["name"])
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/dashboard/test_context.py -v -m integration`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/
git commit -m "#6 feat: add dashboard context queries (stats, changes, queue, rate limiter)"
```

---

## Task 6: Dashboard partial endpoint tests

**Files:**
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Add partial endpoint tests**

Append to `tests/dashboard/test_routes.py`:

```python
class TestPartialEndpoints:
    async def test_stats_cards_partial(self, client):
        response = await client.get("/partials/stats-cards")
        assert response.status_code == 200
        assert b"Total Watches" in response.content

    async def test_recent_changes_partial(self, client):
        response = await client.get("/partials/recent-changes")
        assert response.status_code == 200

    async def test_system_health_partial(self, client):
        response = await client.get("/partials/system-health")
        assert response.status_code == 200
        assert b"Task Queue" in response.content
```

- [ ] **Step 2: Run all dashboard tests**

Run: `uv run pytest tests/dashboard/ -v -m integration`
Expected: PASS (6 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/dashboard/test_routes.py
git commit -m "#6 test: add partial endpoint tests for dashboard"
```

---

## Task 7: Build CSS and update documentation

**Files:**
- Run: `scripts/build-css.sh`
- Modify: `AGENTS.md`
- Modify: `docs/COMMANDS.md`

- [ ] **Step 1: Build production CSS**

```bash
bash scripts/build-css.sh
```

- [ ] **Step 2: Update AGENTS.md**

Add to project layout:
```
src/dashboard/           — Server-rendered dashboard (Jinja2 + HTMX + Tailwind)
src/dashboard/routes.py  — Dashboard page and partial routes
src/dashboard/context.py — Dashboard-specific DB query helpers
src/dashboard/static/    — CSS, JS (vendored HTMX), compiled Tailwind
src/dashboard/templates/ — Jinja2 templates (base, pages, partials)
scripts/                 — Build scripts (Tailwind CSS)
```

Add to common commands:
```bash
# Build Tailwind CSS
bash scripts/build-css.sh

# Watch mode (auto-rebuild on changes)
bash scripts/build-css.sh --watch
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest -v
uv run pytest -m integration -v
uv run ruff check .
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md docs/COMMANDS.md src/dashboard/static/css/output.css
git commit -m "#6 docs: add dashboard to project layout and commands"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Dependencies + Tailwind CLI | — |
| 2 | Pre-commit CSS validation hook | — |
| 3 | Static files + vendored HTMX | — |
| 4 | Base template, routes, registration | 3 integration |
| 5 | Context queries (stats, changes, queue, domains) | 5 integration |
| 6 | Partial endpoint tests | 3 integration |
| 7 | CSS build + documentation | regression check |

Total: ~11 new tests. After this phase, the dashboard home page is functional with live stats, recent changes, and system health — all auto-refreshing via HTMX.
