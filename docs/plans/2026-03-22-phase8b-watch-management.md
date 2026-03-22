# Phase 8b: Watch Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard pages for listing, viewing, creating, editing, and deactivating watches — with status indicators, change history, and inline HTMX actions.

**Architecture:** New routes in `src/dashboard/routes.py`, new context queries in `src/dashboard/context.py`, new templates for each page. Forms use standard HTML POST with server-side validation and redirect on success. Deactivate uses HTMX for inline updates.

**Tech Stack:** Jinja2, HTMX, Tailwind CSS, existing SQLAlchemy models

**Design doc:** `docs/plans/2026-03-21-phase8-dashboard-design.md`
**Issue:** #6

---

## File Structure

```
src/dashboard/
  routes.py                          — modify: add watch list/detail/create/edit/deactivate routes
  context.py                         — modify: add watch list/detail/change history queries
  templates/
    pages/
      watches.html                   — create: watch list page
      watch_detail.html              — create: watch detail page
      watch_form.html                — create: create/edit form
    partials/
      watch_table.html               — create: watch list table (HTMX-swappable)
      watch_row.html                 — create: single watch row (for HTMX swap on deactivate)
      watch_changes.html             — create: change history for a watch
      flash.html                     — create: flash message partial
tests/dashboard/
  test_routes.py                     — modify: add watch management tests
  test_context.py                    — modify: add watch context query tests
```

---

## Task 1: Watch list context queries

**Files:**
- Modify: `src/dashboard/context.py`
- Modify: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/dashboard/test_context.py`:

```python
from src.dashboard.context import get_watch_list


class TestGetWatchList:
    async def test_empty(self, db_session):
        watches = await get_watch_list(db_session)
        assert watches == []

    async def test_returns_watches_with_status(self, db_session):
        watch = Watch(name="W1", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()
        watches = await get_watch_list(db_session)
        assert len(watches) == 1
        assert watches[0].name == "W1"
        assert watches[0].last_checked_at is None

    async def test_filter_active(self, db_session):
        db_session.add(Watch(name="Active", url="https://a.com", content_type="html"))
        db_session.add(Watch(name="Inactive", url="https://b.com", content_type="html", is_active=False))
        await db_session.flush()
        active = await get_watch_list(db_session, is_active=True)
        assert len(active) == 1
        assert active[0].name == "Active"
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement get_watch_list**

Add to `src/dashboard/context.py`:

```python
async def get_watch_list(
    session: AsyncSession, is_active: bool | None = None
) -> list[Watch]:
    """Fetch watches for list display, optionally filtered by active status."""
    stmt = select(Watch).order_by(Watch.created_at.desc())
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#6 feat: add get_watch_list context query"
```

---

## Task 2: Watch detail context queries

**Files:**
- Modify: `src/dashboard/context.py`
- Modify: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests**

```python
from src.dashboard.context import get_watch_detail, get_watch_changes


class TestGetWatchDetail:
    async def test_returns_watch(self, db_session):
        watch = Watch(name="Detail", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()
        result = await get_watch_detail(db_session, str(watch.id))
        assert result is not None
        assert result.name == "Detail"

    async def test_not_found(self, db_session):
        result = await get_watch_detail(db_session, "01JNXXXXXXXXXXXXXXXXXXXXXXXXX")
        assert result is None

    async def test_invalid_ulid(self, db_session):
        result = await get_watch_detail(db_session, "not-a-ulid")
        assert result is None


class TestGetWatchChanges:
    async def test_empty(self, db_session):
        watch = Watch(name="NoChanges", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()
        changes = await get_watch_changes(db_session, str(watch.id))
        assert changes == []
```

- [ ] **Step 2: Implement**

```python
from ulid import ULID


async def get_watch_detail(session: AsyncSession, watch_id: str) -> Watch | None:
    """Fetch a single watch by ID string. Returns None if not found or invalid."""
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return None
    return await session.get(Watch, parsed)


async def get_watch_changes(
    session: AsyncSession, watch_id: str, limit: int = 50
) -> list[dict]:
    """Fetch change history for a specific watch."""
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return []
    stmt = (
        select(Change)
        .where(Change.watch_id == parsed)
        .order_by(Change.detected_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    changes = []
    for change in result.scalars().all():
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
        changes.append({
            "id": str(change.id),
            "detected_at": change.detected_at,
            "summary": ", ".join(parts) if parts else "change detected",
        })
    return changes
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
git commit -m "#6 feat: add get_watch_detail and get_watch_changes context queries"
```

---

## Task 3: Watch list page and table partial

**Files:**
- Create: `src/dashboard/templates/pages/watches.html`
- Create: `src/dashboard/templates/partials/watch_table.html`
- Create: `src/dashboard/templates/partials/watch_row.html`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/dashboard/test_routes.py`:

```python
class TestWatchList:
    async def test_watches_page_returns_200(self, client):
        response = await client.get("/watches")
        assert response.status_code == 200
        assert b"Watches" in response.content

    async def test_watches_page_has_create_link(self, client):
        response = await client.get("/watches")
        assert b"/watches/new" in response.content

    async def test_watch_table_partial(self, client):
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200

    async def test_watch_table_filter(self, client):
        response = await client.get("/partials/watch-table?is_active=true")
        assert response.status_code == 200
```

- [ ] **Step 2: Create templates**

Create `src/dashboard/templates/partials/watch_row.html`:
```html
<tr id="watch-{{ watch.id }}">
  <td>
    <a href="/watches/{{ watch.id }}" class="text-blue-600 hover:underline font-medium">{{ watch.name }}</a>
  </td>
  <td>
    <span class="truncate max-w-[300px] inline-block text-gray-500" title="{{ watch.url }}">{{ watch.url }}</span>
  </td>
  <td>
    <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium
      {% if watch.content_type == 'html' %}bg-blue-100 text-blue-800
      {% elif watch.content_type == 'pdf' %}bg-red-100 text-red-800
      {% else %}bg-green-100 text-green-800{% endif %}">
      {{ watch.content_type }}
    </span>
  </td>
  <td>
    {% if watch.is_active %}
      <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">Active</span>
    {% else %}
      <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-500">Inactive</span>
    {% endif %}
  </td>
  <td class="text-gray-500">
    {% if watch.last_checked_at %}{{ watch.last_checked_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}Never{% endif %}
  </td>
  <td>
    {% if watch.is_active %}
    <button
      hx-post="/watches/{{ watch.id }}/deactivate"
      hx-target="#watch-{{ watch.id }}"
      hx-swap="outerHTML"
      hx-confirm="Deactivate {{ watch.name }}?"
      class="text-xs text-red-600 hover:text-red-800">
      Deactivate
    </button>
    {% endif %}
  </td>
</tr>
```

Create `src/dashboard/templates/partials/watch_table.html`:
```html
{% if watches %}
<table class="data-table">
  <thead>
    <tr>
      <th>Name</th>
      <th>URL</th>
      <th>Type</th>
      <th>Status</th>
      <th>Last Checked</th>
      <th></th>
    </tr>
  </thead>
  <tbody class="divide-y divide-gray-100">
    {% for watch in watches %}
      {% include "partials/watch_row.html" %}
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="text-gray-500 text-sm">No watches found.</p>
{% endif %}
```

Create `src/dashboard/templates/pages/watches.html`:
```html
{% extends "base.html" %}
{% block title %}Watches — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <h2 class="text-2xl font-bold text-gray-900">Watches</h2>
  <a href="/watches/new" class="inline-flex items-center px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700">
    New Watch
  </a>
</div>

<div class="flex gap-2 mb-4">
  <button hx-get="/partials/watch-table" hx-target="#watch-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">All</button>
  <button hx-get="/partials/watch-table?is_active=true" hx-target="#watch-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">Active</button>
  <button hx-get="/partials/watch-table?is_active=false" hx-target="#watch-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">Inactive</button>
</div>

<div id="watch-table">
  {% include "partials/watch_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 3: Add routes**

Add to `src/dashboard/routes.py`:

```python
from src.dashboard.context import get_watch_list


@router.get("/watches")
async def watches_page(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch list page."""
    watches = await get_watch_list(session, is_active=is_active)
    context = {"request": request, "active_page": "watches", "watches": watches}
    return templates.TemplateResponse("pages/watches.html", context)


@router.get("/partials/watch-table")
async def partial_watch_table(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watch table with optional filter."""
    watches = await get_watch_list(session, is_active=is_active)
    return templates.TemplateResponse(
        "partials/watch_table.html", {"request": request, "watches": watches}
    )
```

- [ ] **Step 4: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add watch list page with filtering and status indicators"
```

---

## Task 4: Watch detail page

**Files:**
- Create: `src/dashboard/templates/pages/watch_detail.html`
- Create: `src/dashboard/templates/partials/watch_changes.html`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
class TestWatchDetail:
    async def test_detail_page_returns_200(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Detail Watch", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert b"Detail Watch" in response.content

    async def test_detail_page_404_invalid(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404

    async def test_detail_page_has_edit_link(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Edit Link", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}")
        assert f"/watches/{watch_id}/edit".encode() in response.content
```

- [ ] **Step 2: Create templates**

Create `src/dashboard/templates/partials/watch_changes.html`:
```html
{% if changes %}
<table class="data-table">
  <thead>
    <tr>
      <th>Detected</th>
      <th>Summary</th>
    </tr>
  </thead>
  <tbody class="divide-y divide-gray-100">
    {% for change in changes %}
    <tr>
      <td class="text-gray-500">{{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}</td>
      <td>
        <a href="/changes/{{ change.id }}" class="text-blue-600 hover:underline">{{ change.summary }}</a>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="text-gray-500 text-sm">No changes detected yet.</p>
{% endif %}
```

Create `src/dashboard/templates/pages/watch_detail.html`:
```html
{% extends "base.html" %}
{% block title %}{{ watch.name }} — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <div>
    <h2 class="text-2xl font-bold text-gray-900">{{ watch.name }}</h2>
    <p class="text-sm text-gray-500 mt-1">{{ watch.url }}</p>
  </div>
  <div class="flex gap-2">
    <a href="/watches/{{ watch.id }}/edit" class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50">Edit</a>
    {% if watch.is_active %}
    <button
      hx-post="/watches/{{ watch.id }}/deactivate"
      hx-target="#watch-status"
      hx-swap="innerHTML"
      hx-confirm="Deactivate {{ watch.name }}?"
      class="px-4 py-2 text-sm font-medium text-red-600 bg-white border border-red-300 rounded-md hover:bg-red-50">
      Deactivate
    </button>
    {% endif %}
  </div>
</div>

<!-- Status and config -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 mb-3">Configuration</h3>
    <dl class="space-y-2">
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Content Type</dt>
        <dd class="text-sm font-medium">{{ watch.content_type }}</dd>
      </div>
      <div id="watch-status" class="flex justify-between">
        <dt class="text-sm text-gray-600">Status</dt>
        <dd class="text-sm font-medium {% if watch.is_active %}text-green-600{% else %}text-gray-500{% endif %}">
          {{ "Active" if watch.is_active else "Inactive" }}
        </dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Check Interval</dt>
        <dd class="text-sm font-medium">{{ watch.schedule_config.get('interval', '1d') if watch.schedule_config else '1d' }}</dd>
      </div>
      <div class="flex justify-between">
        <dt class="text-sm text-gray-600">Last Checked</dt>
        <dd class="text-sm font-medium">
          {% if watch.last_checked_at %}{{ watch.last_checked_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}Never{% endif %}
        </dd>
      </div>
    </dl>
  </div>

  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 mb-3">Fetch Config</h3>
    {% if watch.fetch_config %}
    <pre class="text-xs text-gray-600 bg-gray-50 p-3 rounded overflow-x-auto">{{ watch.fetch_config | tojson(indent=2) }}</pre>
    {% else %}
    <p class="text-gray-500 text-sm">Default configuration</p>
    {% endif %}
  </div>
</div>

<!-- Temporal Profiles (read-only) -->
{% if profiles %}
<div class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 mb-4">Temporal Profiles</h3>
  <div class="space-y-2">
    {% for profile in profiles %}
    <div class="stat-card text-sm">
      <span class="font-medium">{{ profile.profile_type }}</span>
      {% if profile.reference_date %} — {{ profile.reference_date }}{% endif %}
      {% if profile.date_range_start %} — {{ profile.date_range_start }} to {{ profile.date_range_end }}{% endif %}
      <span class="text-gray-500 ml-2">({{ profile.post_action }})</span>
      {% if not profile.is_active %}<span class="text-gray-400 ml-1">[inactive]</span>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- Notification Configs (read-only) -->
{% if notifications %}
<div class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 mb-4">Notifications</h3>
  <div class="space-y-2">
    {% for nc in notifications %}
    <div class="stat-card text-sm">
      <span class="font-medium">{{ nc.channel }}</span>
      {% if not nc.is_active %}<span class="text-gray-400 ml-1">[inactive]</span>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}

<!-- Change history -->
<div>
  <h3 class="text-lg font-semibold text-gray-900 mb-4">Change History</h3>
  <div id="watch-changes" hx-get="/partials/watch-changes/{{ watch.id }}" hx-trigger="every 30s" hx-swap="innerHTML">
    {% include "partials/watch_changes.html" %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 3: Add routes**

```python
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from src.core.models.notification_config import NotificationConfig
from src.core.models.temporal_profile import TemporalProfile
from src.dashboard.context import get_watch_detail, get_watch_changes


@router.get("/watches/{watch_id}")
async def watch_detail_page(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch detail page with profiles, notifications, and change history."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    changes = await get_watch_changes(session, watch_id)

    # Load profiles and notification configs
    profiles_result = await session.execute(
        select(TemporalProfile).where(TemporalProfile.watch_id == watch.id)
    )
    profiles = list(profiles_result.scalars().all())
    nc_result = await session.execute(
        select(NotificationConfig).where(NotificationConfig.watch_id == watch.id)
    )
    notifications = list(nc_result.scalars().all())

    context = {
        "request": request,
        "active_page": "watches",
        "watch": watch,
        "changes": changes,
        "profiles": profiles,
        "notifications": notifications,
    }
    return templates.TemplateResponse("pages/watch_detail.html", context)


@router.get("/partials/watch-changes/{watch_id}")
async def partial_watch_changes(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: change history for a watch."""
    changes = await get_watch_changes(session, watch_id)
    return templates.TemplateResponse(
        "partials/watch_changes.html", {"request": request, "changes": changes}
    )
```

- [ ] **Step 4: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add watch detail page with change history"
```

---

## Task 5: Watch create/edit form

**Files:**
- Create: `src/dashboard/templates/pages/watch_form.html`
- Create: `src/dashboard/templates/partials/flash.html`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
class TestWatchCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        assert b"New Watch" in response.content

    async def test_create_form_has_fields(self, client):
        response = await client.get("/watches/new")
        assert b'name="name"' in response.content
        assert b'name="url"' in response.content
        assert b'name="content_type"' in response.content

    async def test_create_watch_redirects(self, client):
        response = await client.post("/watches/new", data={
            "name": "Created Watch",
            "url": "https://example.com",
            "content_type": "html",
        }, follow_redirects=False)
        assert response.status_code == 303

    async def test_create_watch_missing_name_shows_error(self, client):
        response = await client.post("/watches/new", data={
            "name": "",
            "url": "https://example.com",
            "content_type": "html",
        })
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()


class TestWatchEdit:
    async def test_edit_form_returns_200(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Editable", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}/edit")
        assert response.status_code == 200
        assert b"Editable" in response.content

    async def test_edit_form_prefills(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Prefilled", "url": "https://prefilled.com", "content_type": "pdf",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}/edit")
        assert b"Prefilled" in response.content
        assert b"https://prefilled.com" in response.content

    async def test_edit_watch_redirects(self, client):
        resp = await client.post("/api/watches", json={
            "name": "ToEdit", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.post(f"/watches/{watch_id}/edit", data={
            "name": "Edited Name",
            "url": "https://edited.com",
            "content_type": "html",
        }, follow_redirects=False)
        assert response.status_code == 303
```

- [ ] **Step 2: Create flash partial**

Create `src/dashboard/templates/partials/flash.html`:
```html
{% if flash %}
<div data-auto-dismiss class="mb-4 p-4 rounded-md
  {% if flash.type == 'success' %}bg-green-50 text-green-800 border border-green-200
  {% elif flash.type == 'error' %}bg-red-50 text-red-800 border border-red-200
  {% else %}bg-blue-50 text-blue-800 border border-blue-200{% endif %}">
  {{ flash.message }}
</div>
{% endif %}
```

- [ ] **Step 3: Create watch form template**

Create `src/dashboard/templates/pages/watch_form.html`:
```html
{% extends "base.html" %}
{% block title %}{{ "Edit" if watch else "New" }} Watch — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 mb-6">{{ "Edit" if watch else "New" }} Watch</h2>

{% include "partials/flash.html" %}

<form method="post" class="max-w-xl space-y-6">
  <div>
    <label for="name" class="block text-sm font-medium text-gray-700">Name</label>
    <input type="text" name="name" id="name" required
      value="{{ watch.name if watch else '' }}"
      class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 text-sm p-2 border">
  </div>

  <div>
    <label for="url" class="block text-sm font-medium text-gray-700">URL</label>
    <input type="url" name="url" id="url" required
      value="{{ watch.url if watch else '' }}"
      class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 text-sm p-2 border">
  </div>

  <div>
    <label for="content_type" class="block text-sm font-medium text-gray-700">Content Type</label>
    <select name="content_type" id="content_type"
      class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 text-sm p-2 border">
      {% for ct in ["html", "pdf", "file"] %}
      <option value="{{ ct }}" {% if watch and watch.content_type == ct %}selected{% endif %}>{{ ct | upper }}</option>
      {% endfor %}
    </select>
  </div>

  <div>
    <label for="interval" class="block text-sm font-medium text-gray-700">Check Interval</label>
    <input type="text" name="interval" id="interval" placeholder="1d"
      value="{{ watch.schedule_config.get('interval', '') if watch and watch.schedule_config else '' }}"
      class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 text-sm p-2 border">
    <p class="mt-1 text-xs text-gray-500">Format: 30s, 15m, 6h, 1d</p>
  </div>

  <div class="flex gap-3">
    <button type="submit" class="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700">
      {{ "Save Changes" if watch else "Create Watch" }}
    </button>
    <a href="{{ '/watches/' ~ watch.id if watch else '/watches' }}" class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50">
      Cancel
    </a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Add routes**

```python
from fastapi import Form
from fastapi.responses import RedirectResponse
from src.core.models.audit_log import AuditLog
from src.core.models.watch import ContentType, Watch


@router.get("/watches/new")
async def watch_create_form(request: Request):
    """Watch creation form."""
    return templates.TemplateResponse(
        "pages/watch_form.html",
        {"request": request, "active_page": "watches", "watch": None, "flash": None},
    )


@router.post("/watches/new")
async def watch_create_submit(
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    content_type: str = Form("html"),
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Handle watch creation form submission."""
    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not url.strip():
        errors.append("URL is required")

    if errors:
        flash = {"type": "error", "message": ". ".join(errors)}
        return templates.TemplateResponse(
            "pages/watch_form.html",
            {"request": request, "active_page": "watches", "watch": None, "flash": flash},
        )

    schedule_config = {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()

    watch = Watch(
        name=name.strip(),
        url=url.strip(),
        content_type=content_type,
        schedule_config=schedule_config,
    )
    session.add(watch)
    session.add(AuditLog(
        event_type="watch.created",
        watch_id=watch.id,
        payload={"name": name, "url": url, "source": "dashboard"},
    ))
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch.id}", status_code=303)


@router.get("/watches/{watch_id}/edit")
async def watch_edit_form(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch edit form, prefilled with current values."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    return templates.TemplateResponse(
        "pages/watch_form.html",
        {"request": request, "active_page": "watches", "watch": watch, "flash": None},
    )


@router.post("/watches/{watch_id}/edit")
async def watch_edit_submit(
    request: Request,
    watch_id: str,
    name: str = Form(""),
    url: str = Form(""),
    content_type: str = Form("html"),
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Handle watch edit form submission."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")

    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not url.strip():
        errors.append("URL is required")

    if errors:
        flash = {"type": "error", "message": ". ".join(errors)}
        return templates.TemplateResponse(
            "pages/watch_form.html",
            {"request": request, "active_page": "watches", "watch": watch, "flash": flash},
        )

    watch.name = name.strip()
    watch.url = url.strip()
    watch.content_type = content_type
    schedule_config = watch.schedule_config or {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()
    watch.schedule_config = schedule_config

    session.add(AuditLog(
        event_type="watch.updated",
        watch_id=watch.id,
        payload={"updated_fields": ["name", "url", "content_type", "schedule_config"], "source": "dashboard"},
    ))
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch.id}", status_code=303)
```

**IMPORTANT:** The `/watches/new` route must be registered BEFORE `/watches/{watch_id}` in the router — otherwise FastAPI interprets "new" as a watch_id. Move the create routes above the detail route.

- [ ] **Step 5: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add watch create/edit forms with validation"
```

---

## Task 6: Deactivate via HTMX

**Files:**
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing test**

```python
class TestWatchDeactivate:
    async def test_deactivate_returns_updated_row(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Deactivate Me", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.post(f"/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert b"Inactive" in response.content
```

- [ ] **Step 2: Add route**

```python
@router.post("/watches/{watch_id}/deactivate")
async def watch_deactivate(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch via HTMX — returns updated row or status snippet."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    watch.is_active = False
    session.add(AuditLog(
        event_type="watch.deactivated",
        watch_id=watch.id,
        payload={"name": watch.name, "source": "dashboard"},
    ))
    await session.commit()
    await session.refresh(watch)

    # Detail page targets #watch-status; list page targets #watch-{id} row
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "watch-status":
        html = '<dt class="text-sm text-gray-600">Status</dt>'
        html += '<dd class="text-sm font-medium text-gray-500">Inactive</dd>'
        return HTMLResponse(content=html)
    return templates.TemplateResponse(
        "partials/watch_row.html", {"request": request, "watch": watch}
    )
```

- [ ] **Step 3: Run tests, commit**

```bash
git commit -m "#6 feat: add watch deactivate via HTMX"
```

---

## Task 7: Rebuild CSS and run full suite

- [ ] **Step 1: Rebuild CSS**

```bash
bash scripts/build-css.sh
```

- [ ] **Step 2: Run full suite**

```bash
uv run pytest -v
uv run pytest -m integration -v
uv run ruff check .
```

- [ ] **Step 3: Commit any remaining changes**

```bash
git commit -m "#6 chore: rebuild CSS for watch management pages"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Watch list context query | 3 integration |
| 2 | Watch detail + changes context queries | 4 integration |
| 3 | Watch list page + table partial | 4 integration |
| 4 | Watch detail page + change history | 3 integration |
| 5 | Watch create/edit forms + validation | 6 integration |
| 6 | Deactivate via HTMX | 1 integration |
| 7 | CSS rebuild + full suite | regression |

Total: ~21 new tests
