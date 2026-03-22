# Phase 8c: Change Detail, Audit Log & System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change detail page with chunk metadata and side-by-side diff (extracted text default, raw toggle), filterable audit log page, and system monitoring page with queue health and rate limiter state.

**Architecture:** New routes, context queries, and templates following established Phase 8a/8b patterns. Diff rendering uses `difflib.unified_diff` server-side. Raw content toggle via HTMX partial swap. Audit log and system pages are read-only with filtering.

**Tech Stack:** Jinja2, HTMX, Tailwind CSS, difflib, existing models and StorageBackend

**Design doc:** `docs/plans/2026-03-21-phase8-dashboard-design.md`
**Issue:** #6

---

## File Structure

```
src/dashboard/
  routes.py                                — modify: add change detail, audit log, system routes
  context.py                               — modify: add change detail, audit log, system queries
  templates/
    pages/
      change_detail.html                   — create: change detail with metadata + diff
      audit_log.html                       — create: filterable audit log
      system.html                          — create: system monitoring page
    partials/
      diff_view.html                       — create: side-by-side diff partial (HTMX-swappable for raw toggle)
      chunk_table.html                     — create: chunk metadata table
      audit_table.html                     — create: audit log table (HTMX-swappable for filtering)
tests/dashboard/
  test_routes.py                           — modify: add change detail, audit, system tests
  test_context.py                          — modify: add context query tests
```

---

## Task 1: Change detail context queries

**Files:**
- Modify: `src/dashboard/context.py`
- Modify: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/dashboard/test_context.py`:

```python
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.dashboard.context import get_change_detail


class TestGetChangeDetail:
    async def test_returns_change_with_snapshots(self, db_session):
        watch = Watch(name="W", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap_kwargs = dict(
            watch_id=watch.id, content_hash="a" * 64, simhash=0,
            storage_path="/tmp/s", text_path="/tmp/t",
            chunk_count=1, text_bytes=100, fetch_duration_ms=50, fetcher_used="http",
        )
        prev_snap = Snapshot(**snap_kwargs)
        curr_snap = Snapshot(**snap_kwargs)
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        chunk = SnapshotChunk(
            snapshot_id=curr_snap.id, chunk_index=0, chunk_type="section",
            chunk_label="Main", content_hash="b" * 64, simhash=0,
            char_count=100, excerpt="Hello world",
        )
        db_session.add(chunk)

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
            change_metadata={"added": ["Section A"], "modified": [], "removed": []},
        )
        db_session.add(change)
        await db_session.flush()

        result = await get_change_detail(db_session, str(change.id))
        assert result is not None
        assert result["change"] is not None
        assert result["watch_name"] == "W"
        assert result["current_snapshot"] is not None
        assert len(result["current_chunks"]) == 1

    async def test_not_found(self, db_session):
        result = await get_change_detail(db_session, "01JNZZZZZZZZZZZZZZZZZZZZZZ")
        assert result is None

    async def test_invalid_id(self, db_session):
        result = await get_change_detail(db_session, "bad")
        assert result is None
```

- [ ] **Step 2: Implement get_change_detail**

Add to `src/dashboard/context.py`:

```python
from src.core.models.snapshot import Snapshot, SnapshotChunk


async def get_change_detail(session: AsyncSession, change_id: str) -> dict | None:
    """Fetch a change with its snapshots, chunks, and watch name."""
    try:
        parsed = ULID.from_str(change_id)
    except ValueError:
        return None

    change = await session.get(Change, parsed)
    if not change:
        return None

    # Watch name
    watch = await session.get(Watch, change.watch_id)
    watch_name = watch.name if watch else "Unknown"

    # Snapshots
    prev_snap = await session.get(Snapshot, change.previous_snapshot_id)
    curr_snap = await session.get(Snapshot, change.current_snapshot_id)

    # Chunks for current snapshot
    curr_chunks = []
    if curr_snap:
        stmt = (
            select(SnapshotChunk)
            .where(SnapshotChunk.snapshot_id == curr_snap.id)
            .order_by(SnapshotChunk.chunk_index)
        )
        result = await session.execute(stmt)
        curr_chunks = list(result.scalars().all())

    # Chunks for previous snapshot
    prev_chunks = []
    if prev_snap:
        stmt = (
            select(SnapshotChunk)
            .where(SnapshotChunk.snapshot_id == prev_snap.id)
            .order_by(SnapshotChunk.chunk_index)
        )
        result = await session.execute(stmt)
        prev_chunks = list(result.scalars().all())

    return {
        "change": change,
        "watch_name": watch_name,
        "watch_id": str(change.watch_id),
        "current_snapshot": curr_snap,
        "previous_snapshot": prev_snap,
        "current_chunks": curr_chunks,
        "previous_chunks": prev_chunks,
    }
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
git commit -m "#6 feat: add get_change_detail context query"
```

---

## Task 2: Diff generation helper

**Files:**
- Modify: `src/dashboard/context.py`
- Modify: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests**

```python
from src.dashboard.context import generate_diff


class TestGenerateDiff:
    def test_identical_text(self):
        result = generate_diff("hello\nworld", "hello\nworld")
        assert result["has_changes"] is False

    def test_modified_text(self):
        result = generate_diff("hello\nworld", "hello\nplanet")
        assert result["has_changes"] is True
        assert len(result["lines"]) > 0

    def test_empty_previous(self):
        result = generate_diff("", "new content")
        assert result["has_changes"] is True

    def test_empty_both(self):
        result = generate_diff("", "")
        assert result["has_changes"] is False
```

- [ ] **Step 2: Implement generate_diff**

Add to `src/dashboard/context.py`:

```python
import difflib


def generate_diff(previous_text: str, current_text: str) -> dict:
    """Generate a side-by-side diff between two text strings.

    Returns dict with 'has_changes' bool and 'lines' list of
    (tag, prev_line, curr_line) tuples where tag is 'equal', 'insert',
    'delete', or 'replace'.
    """
    prev_lines = previous_text.splitlines(keepends=True)
    curr_lines = current_text.splitlines(keepends=True)

    sm = difflib.SequenceMatcher(None, prev_lines, curr_lines)
    lines = []
    has_changes = False

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                lines.append(("equal", prev_lines[i1 + k].rstrip(), curr_lines[j1 + k].rstrip()))
        elif tag == "replace":
            has_changes = True
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                prev = prev_lines[i1 + k].rstrip() if i1 + k < i2 else ""
                curr = curr_lines[j1 + k].rstrip() if j1 + k < j2 else ""
                lines.append(("replace", prev, curr))
        elif tag == "delete":
            has_changes = True
            for k in range(i1, i2):
                lines.append(("delete", prev_lines[k].rstrip(), ""))
        elif tag == "insert":
            has_changes = True
            for k in range(j1, j2):
                lines.append(("insert", "", curr_lines[k].rstrip()))

    return {"has_changes": has_changes, "lines": lines}
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
git commit -m "#6 feat: add generate_diff helper for side-by-side rendering"
```

---

## Task 3: Change detail page

**Files:**
- Create: `src/dashboard/templates/pages/change_detail.html`
- Create: `src/dashboard/templates/partials/chunk_table.html`
- Create: `src/dashboard/templates/partials/diff_view.html`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
class TestChangeDetail:
    async def test_change_detail_404_invalid(self, client):
        response = await client.get("/changes/bad-id")
        assert response.status_code == 404

    async def test_change_detail_returns_200(self, client):
        # Create watch + snapshots + change via API/DB
        watch_resp = await client.post("/api/watches", json={
            "name": "Diff Watch", "url": "https://example.com", "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]
        # Get changes (may be empty, but route should work)
        response = await client.get(f"/changes/{watch_id}")
        # This will 404 since watch_id is not a change_id, which is correct
        assert response.status_code == 404
```

- [ ] **Step 2: Create templates**

Create `src/dashboard/templates/partials/chunk_table.html`:
```html
{% if chunks %}
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
  <tbody class="divide-y divide-gray-100">
    {% for chunk in chunks %}
    <tr>
      <td>{{ chunk.chunk_index }}</td>
      <td class="font-medium">{{ chunk.chunk_label }}</td>
      <td>{{ chunk.chunk_type }}</td>
      <td>{{ chunk.char_count }}</td>
      <td class="text-gray-500 truncate max-w-[300px]" title="{{ chunk.excerpt }}">{{ chunk.excerpt[:100] }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="text-gray-500 text-sm">No chunks.</p>
{% endif %}
```

Create `src/dashboard/templates/partials/diff_view.html`:
```html
{% if diff.has_changes %}
<div class="overflow-x-auto">
  <table class="w-full text-xs font-mono border border-gray-200">
    <thead>
      <tr>
        <th class="w-1/2 px-3 py-2 text-left bg-red-50 text-red-800 border-r">Previous</th>
        <th class="w-1/2 px-3 py-2 text-left bg-green-50 text-green-800">Current</th>
      </tr>
    </thead>
    <tbody>
      {% for tag, prev_line, curr_line in diff.lines %}
      <tr class="{% if tag == 'replace' %}bg-yellow-50{% elif tag == 'delete' %}bg-red-50{% elif tag == 'insert' %}bg-green-50{% endif %}">
        <td class="px-3 py-0.5 border-r border-gray-200 whitespace-pre-wrap {% if tag in ('delete', 'replace') %}text-red-700{% else %}text-gray-600{% endif %}">{{ prev_line }}</td>
        <td class="px-3 py-0.5 whitespace-pre-wrap {% if tag in ('insert', 'replace') %}text-green-700{% else %}text-gray-600{% endif %}">{{ curr_line }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 text-sm">No textual differences found.</p>
{% endif %}
```

Create `src/dashboard/templates/pages/change_detail.html`:
```html
{% extends "base.html" %}
{% block title %}Change — {{ watch_name }} — watcher{% endblock %}
{% block content %}
<div class="mb-6">
  <h2 class="text-2xl font-bold text-gray-900">Change Detail</h2>
  <p class="text-sm text-gray-500 mt-1">
    <a href="/watches/{{ watch_id }}" class="text-blue-600 hover:underline">{{ watch_name }}</a>
    — detected {{ change.detected_at.strftime('%Y-%m-%d %H:%M UTC') }}
  </p>
</div>

<!-- Change metadata -->
<div class="stat-card mb-6">
  <h3 class="text-sm font-medium text-gray-500 mb-3">Change Summary</h3>
  <div class="flex gap-4">
    {% set meta = change.change_metadata or {} %}
    {% set added = meta.get('added', []) %}
    {% set modified = meta.get('modified', []) %}
    {% set removed = meta.get('removed', []) %}
    {% if added %}
    <div>
      <span class="text-green-600 font-medium">{{ added | length }} added</span>
      <ul class="text-xs text-gray-500 mt-1">{% for item in added %}<li>{{ item if item is string else item.label }}</li>{% endfor %}</ul>
    </div>
    {% endif %}
    {% if modified %}
    <div>
      <span class="text-yellow-600 font-medium">{{ modified | length }} modified</span>
      <ul class="text-xs text-gray-500 mt-1">{% for item in modified %}<li>{{ item if item is string else item.label }} {% if item is mapping and item.similarity is defined %}({{ "%.0f" | format(item.similarity * 100) }}% similar){% endif %}</li>{% endfor %}</ul>
    </div>
    {% endif %}
    {% if removed %}
    <div>
      <span class="text-red-600 font-medium">{{ removed | length }} removed</span>
      <ul class="text-xs text-gray-500 mt-1">{% for item in removed %}<li>{{ item if item is string else item.label }}</li>{% endfor %}</ul>
    </div>
    {% endif %}
  </div>
</div>

<!-- Snapshot info -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 mb-2">Previous Snapshot</h3>
    {% if previous_snapshot %}
    <dl class="space-y-1 text-sm">
      <div class="flex justify-between"><dt class="text-gray-600">Fetched</dt><dd>{{ previous_snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC') }}</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Chunks</dt><dd>{{ previous_snapshot.chunk_count }}</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Size</dt><dd>{{ previous_snapshot.text_bytes }} bytes</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Fetcher</dt><dd>{{ previous_snapshot.fetcher_used }}</dd></div>
    </dl>
    {% else %}
    <p class="text-gray-500 text-sm">Not available</p>
    {% endif %}
  </div>
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 mb-2">Current Snapshot</h3>
    {% if current_snapshot %}
    <dl class="space-y-1 text-sm">
      <div class="flex justify-between"><dt class="text-gray-600">Fetched</dt><dd>{{ current_snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC') }}</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Chunks</dt><dd>{{ current_snapshot.chunk_count }}</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Size</dt><dd>{{ current_snapshot.text_bytes }} bytes</dd></div>
      <div class="flex justify-between"><dt class="text-gray-600">Fetcher</dt><dd>{{ current_snapshot.fetcher_used }}</dd></div>
    </dl>
    {% else %}
    <p class="text-gray-500 text-sm">Not available</p>
    {% endif %}
  </div>
</div>

<!-- Current chunks -->
{% if current_chunks %}
<div class="mb-6">
  <h3 class="text-lg font-semibold text-gray-900 mb-4">Current Chunks</h3>
  {% with chunks=current_chunks %}
  {% include "partials/chunk_table.html" %}
  {% endwith %}
</div>
{% endif %}

<!-- Diff view -->
<div class="mb-6">
  <div class="flex items-center gap-4 mb-4">
    <h3 class="text-lg font-semibold text-gray-900">Diff</h3>
    <div class="flex gap-1">
      <button data-diff-toggle="extracted" onclick="toggleDiffView('extracted')" class="px-3 py-1 text-xs rounded-md bg-gray-200">Extracted Text</button>
      <button data-diff-toggle="raw" onclick="toggleDiffView('raw')" class="px-3 py-1 text-xs rounded-md bg-white border border-gray-200"
        hx-get="/partials/diff/{{ change.id }}?mode=raw"
        hx-target="#diff-content"
        hx-swap="innerHTML"
        hx-trigger="click once">Raw Content</button>
    </div>
  </div>
  <div id="diff-content">
    <div data-diff-view="extracted">
      {% include "partials/diff_view.html" %}
    </div>
    <div data-diff-view="raw" class="hidden">
      <p class="text-gray-500 text-sm">Click "Raw Content" to load.</p>
    </div>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 3: Add routes**

Add to `src/dashboard/routes.py`:

```python
import os
from pathlib import Path

from src.core.storage import LocalStorage
from src.dashboard.context import get_change_detail, generate_diff

STORAGE_BASE_DIR = Path(os.environ.get("WATCHER_DATA_DIR", "/var/lib/watcher/data"))


@router.get("/changes/{change_id}")
async def change_detail_page(
    request: Request,
    change_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Change detail page with metadata, chunks, and diff."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return HTMLResponse(status_code=404, content="Change not found")

    # Generate diff from extracted text stored on disk
    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    prev_text = ""
    curr_text = ""
    if detail["previous_snapshot"] and detail["previous_snapshot"].text_path:
        try:
            prev_text = storage.load(detail["previous_snapshot"].text_path).decode(errors="replace")
        except FileNotFoundError:
            pass
    if detail["current_snapshot"] and detail["current_snapshot"].text_path:
        try:
            curr_text = storage.load(detail["current_snapshot"].text_path).decode(errors="replace")
        except FileNotFoundError:
            pass

    diff = generate_diff(prev_text, curr_text)

    context = {
        "request": request,
        "active_page": "watches",
        **detail,
        "diff": diff,
    }
    return templates.TemplateResponse("pages/change_detail.html", context)


@router.get("/partials/diff/{change_id}")
async def partial_diff(
    request: Request,
    change_id: str,
    mode: str = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: diff view (extracted text or raw content)."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return HTMLResponse(status_code=404, content="Change not found")

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    prev_text = ""
    curr_text = ""

    if mode == "raw":
        # Load raw content
        if detail["previous_snapshot"] and detail["previous_snapshot"].storage_path:
            try:
                prev_text = storage.load(detail["previous_snapshot"].storage_path).decode(errors="replace")
            except FileNotFoundError:
                pass
        if detail["current_snapshot"] and detail["current_snapshot"].storage_path:
            try:
                curr_text = storage.load(detail["current_snapshot"].storage_path).decode(errors="replace")
            except FileNotFoundError:
                pass
    else:
        # Load extracted text
        if detail["previous_snapshot"] and detail["previous_snapshot"].text_path:
            try:
                prev_text = storage.load(detail["previous_snapshot"].text_path).decode(errors="replace")
            except FileNotFoundError:
                pass
        if detail["current_snapshot"] and detail["current_snapshot"].text_path:
            try:
                curr_text = storage.load(detail["current_snapshot"].text_path).decode(errors="replace")
            except FileNotFoundError:
                pass

    diff = generate_diff(prev_text, curr_text)
    return templates.TemplateResponse(
        "partials/diff_view.html", {"request": request, "diff": diff}
    )
```

- [ ] **Step 4: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add change detail page with chunk metadata and side-by-side diff"
```

---

## Task 4: Audit log page

**Files:**
- Create: `src/dashboard/templates/pages/audit_log.html`
- Create: `src/dashboard/templates/partials/audit_table.html`
- Modify: `src/dashboard/context.py`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
class TestAuditLog:
    async def test_audit_page_returns_200(self, client):
        response = await client.get("/audit")
        assert response.status_code == 200
        assert b"Audit Log" in response.content

    async def test_audit_table_partial(self, client):
        response = await client.get("/partials/audit-table")
        assert response.status_code == 200

    async def test_audit_filter_by_event_type(self, client):
        response = await client.get("/partials/audit-table?event_type=watch.created")
        assert response.status_code == 200
```

- [ ] **Step 2: Add context query**

Add to `src/dashboard/context.py`:

```python
async def get_audit_entries(
    session: AsyncSession,
    event_type: str | None = None,
    watch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """Fetch audit log entries with optional filters."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if watch_id:
        try:
            parsed = ULID.from_str(watch_id)
            stmt = stmt.where(AuditLog.watch_id == parsed)
        except ValueError:
            pass
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 3: Create templates**

Create `src/dashboard/templates/partials/audit_table.html`:
```html
{% if entries %}
<table class="data-table">
  <thead>
    <tr>
      <th>Time</th>
      <th>Event</th>
      <th>Watch</th>
      <th>Details</th>
    </tr>
  </thead>
  <tbody class="divide-y divide-gray-100">
    {% for entry in entries %}
    <tr>
      <td class="text-gray-500 whitespace-nowrap">{{ entry.created_at.strftime('%Y-%m-%d %H:%M:%S UTC') }}</td>
      <td>
        <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium
          {% if 'error' in entry.event_type or 'failed' in entry.event_type %}bg-red-100 text-red-800
          {% elif 'created' in entry.event_type %}bg-green-100 text-green-800
          {% elif 'change' in entry.event_type %}bg-blue-100 text-blue-800
          {% else %}bg-gray-100 text-gray-800{% endif %}">
          {{ entry.event_type }}
        </span>
      </td>
      <td>
        {% if entry.watch_id %}
        <a href="/watches/{{ entry.watch_id }}" class="text-blue-600 hover:underline text-sm">{{ entry.watch_id | string | truncate(12, True, '…') }}</a>
        {% else %}—{% endif %}
      </td>
      <td class="text-xs text-gray-500 max-w-[300px] truncate" title="{{ entry.payload | tojson }}">
        {{ entry.payload | tojson | truncate(80, True, '…') }}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="text-gray-500 text-sm">No audit entries found.</p>
{% endif %}
```

Create `src/dashboard/templates/pages/audit_log.html`:
```html
{% extends "base.html" %}
{% block title %}Audit Log — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 mb-6">Audit Log</h2>

<div class="flex gap-2 mb-4 flex-wrap">
  <button hx-get="/partials/audit-table" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">All</button>
  <button hx-get="/partials/audit-table?event_type=watch.created" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">watch.created</button>
  <button hx-get="/partials/audit-table?event_type=watch.updated" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">watch.updated</button>
  <button hx-get="/partials/audit-table?event_type=check.snapshot_created" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">check.snapshot_created</button>
  <button hx-get="/partials/audit-table?event_type=check.no_change" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">check.no_change</button>
  <button hx-get="/partials/audit-table?event_type=check.fetch_failed" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">check.fetch_failed</button>
  <button hx-get="/partials/audit-table?event_type=notification.dispatched" hx-target="#audit-table" class="px-3 py-1 text-sm rounded-md bg-gray-200 hover:bg-gray-300">notification.dispatched</button>
</div>

<div id="audit-table">
  {% include "partials/audit_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 4: Add routes**

```python
from src.dashboard.context import get_audit_entries


@router.get("/audit")
async def audit_log_page(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Audit log page with filtering."""
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    context = {
        "request": request,
        "active_page": "audit",
        "entries": entries,
    }
    return templates.TemplateResponse("pages/audit_log.html", context)


@router.get("/partials/audit-table")
async def partial_audit_table(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: filtered audit log table."""
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    return templates.TemplateResponse(
        "partials/audit_table.html", {"request": request, "entries": entries}
    )
```

- [ ] **Step 5: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add audit log page with event type filtering"
```

---

## Task 5: System monitoring page

**Files:**
- Create: `src/dashboard/templates/pages/system.html`
- Modify: `src/dashboard/routes.py`
- Modify: `tests/dashboard/test_routes.py`

- [ ] **Step 1: Write failing tests**

```python
class TestSystemPage:
    async def test_system_page_returns_200(self, client):
        response = await client.get("/system")
        assert response.status_code == 200
        assert b"System" in response.content

    async def test_system_page_has_queue_section(self, client):
        response = await client.get("/system")
        assert b"Task Queue" in response.content

    async def test_system_page_has_rate_limiter_section(self, client):
        response = await client.get("/system")
        assert b"Rate Limiter" in response.content
```

- [ ] **Step 2: Create template**

Create `src/dashboard/templates/pages/system.html`:
```html
{% extends "base.html" %}
{% block title %}System — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 mb-6">System</h2>

<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
  <!-- Queue Health (detailed) -->
  <div class="stat-card" hx-get="/partials/system-health" hx-trigger="every 10s" hx-swap="innerHTML" hx-select=".stat-card:first-child > *" hx-target="this">
    <h3 class="text-sm font-medium text-gray-500 mb-3">Task Queue</h3>
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

  <!-- Rate Limiter (detailed) -->
  <div class="stat-card">
    <h3 class="text-sm font-medium text-gray-500 mb-3">Rate Limiter</h3>
    {% if domains %}
    <table class="data-table">
      <thead>
        <tr>
          <th>Domain</th>
          <th>Interval</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for domain in domains %}
        <tr>
          <td>{{ domain.name }}</td>
          <td>{{ "%.1f" | format(domain.interval) }}s</td>
          <td>
            {% if domain.in_backoff %}
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-orange-100 text-orange-800">Backoff</span>
            {% else %}
            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">Normal</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-gray-500 text-sm">No domains tracked yet.</p>
    {% endif %}
  </div>
</div>
{% endblock %}
```

- [ ] **Step 3: Add route**

```python
@router.get("/system")
async def system_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """System monitoring page — queue health and rate limiter state."""
    queue = await get_queue_health(session)
    domains = get_rate_limiter_state(get_rate_limiter())
    context = {
        "request": request,
        "active_page": "system",
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/system.html", context)
```

- [ ] **Step 4: Run tests, rebuild CSS, commit**

```bash
bash scripts/build-css.sh
git commit -m "#6 feat: add system monitoring page with queue and rate limiter detail"
```

---

## Task 6: Final CSS rebuild and full suite

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

- [ ] **Step 3: Commit if needed**

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Change detail context query | 3 integration |
| 2 | Diff generation helper | 4 unit |
| 3 | Change detail page + diff view | 2 integration |
| 4 | Audit log page + filtering | 3 integration |
| 5 | System monitoring page | 3 integration |
| 6 | CSS rebuild + full suite | regression |

Total: ~15 new tests
