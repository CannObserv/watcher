# Watch List Search, Filter, and Sort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add name search, domain filter, sortable columns, and Last Changed column to the watches list; add sortable columns and Last Changed to the domain detail watch table; extract a reusable `watch_filters` macro shared by both interfaces.

**Architecture:** Unify filter controls and table into a single partial so sort/filter state is always co-located. The watches list uses `/partials/watch-table` which renders filters + table together inside `#watches-container`. The domain detail adds `/partials/domain-watches/{name}` as a dedicated partial endpoint rendering filters + table inside `#domain-watches`. Both share a `watch_filters` Jinja2 macro. The `is_active` bool param is replaced by `status` string (`"active"` / `"inactive"` / `""`) consistently across both interfaces. Sorting is done server-side via two new params `sort` and `order`.

**Tech Stack:** Python/FastAPI, SQLAlchemy (async), Jinja2 + HTMX, existing `.data-table` / `.segment-group` CSS component classes.

---

## File Map

| Action | File | What changes |
|---|---|---|
| Modify | `src/dashboard/context.py` | `get_watch_list`: add `search`, `domain`, `sort`, `order`; `get_domain_watches`: add `sort`, `order` |
| Modify | `src/dashboard/routes.py` | `watches_page` + `partial_watch_table`: new params; `domain_detail_page`: rename watch_q/watch_status → q/status, add sort/order; new `partial_domain_watches` route |
| Create | `src/dashboard/templates/macros/watch_filters.html` | Reusable search + status + optional domain filter macro |
| Modify | `src/dashboard/templates/partials/watch_table.html` | Include filter macro; remove URL/Type/Deactivate columns; add Last Changed; sortable headers |
| Modify | `src/dashboard/templates/partials/watch_row.html` | Remove URL/Type/Deactivate cells; add `last_changed_at` cell |
| Modify | `src/dashboard/templates/partials/domain_watches_table.html` | Include filter macro; add Last Changed; sortable headers |
| Modify | `src/dashboard/templates/pages/watches.html` | Replace old radio group with `#watches-container` wrapping the partial |
| Modify | `src/dashboard/templates/pages/domain_detail.html` | Remove old filter controls; update `#domain-watches` to use new partial endpoint |
| Modify | `tests/dashboard/test_context.py` | Tests for new `get_watch_list` params; `get_domain_watches` sort |
| Modify | `tests/dashboard/test_routes.py` | Update `name="is_active"` → `name="status"`, `name="watch_status"` → `name="status"`; new tests for search/sort/domain-partial |

---

## Task 1: Extend `get_watch_list()` with search, domain, sort, order

**Files:**
- Modify: `src/dashboard/context.py` (`get_watch_list`, ~line 40)
- Test: `tests/dashboard/test_context.py` (`TestGetWatchList`)

- [ ] **Step 1: Write failing tests**

Add to the `TestGetWatchList` class in `tests/dashboard/test_context.py`:

```python
async def test_search_filters_by_name(self, db_session):
    db_session.add(Watch(name="Alpha Watch", url="https://a.com", content_type="html"))
    db_session.add(Watch(name="Beta Watch", url="https://b.com", content_type="html"))
    await db_session.flush()
    result = await get_watch_list(db_session, search="alpha")
    assert len(result) == 1
    assert result[0].name == "Alpha Watch"

async def test_domain_filters_by_effective_domain(self, db_session):
    db_session.add(Watch(name="W1", url="https://a.com", content_type="html", effective_domain="a.com"))
    db_session.add(Watch(name="W2", url="https://b.com", content_type="html", effective_domain="b.com"))
    await db_session.flush()
    result = await get_watch_list(db_session, domain="a.com")
    assert len(result) == 1
    assert result[0].name == "W1"

async def test_domain_filter_is_partial_match(self, db_session):
    db_session.add(Watch(name="Sub", url="https://sub.example.com", content_type="html", effective_domain="sub.example.com"))
    db_session.add(Watch(name="Root", url="https://example.com", content_type="html", effective_domain="example.com"))
    db_session.add(Watch(name="Other", url="https://other.com", content_type="html", effective_domain="other.com"))
    await db_session.flush()
    result = await get_watch_list(db_session, domain="example")
    names = {w.name for w in result}
    assert "Sub" in names
    assert "Root" in names
    assert "Other" not in names

async def test_sort_by_name_asc(self, db_session):
    db_session.add(Watch(name="Zebra", url="https://a.com", content_type="html"))
    db_session.add(Watch(name="Apple", url="https://b.com", content_type="html"))
    await db_session.flush()
    result = await get_watch_list(db_session, sort="name", order="asc")
    assert result[0].name == "Apple"
    assert result[1].name == "Zebra"

async def test_sort_by_name_desc(self, db_session):
    db_session.add(Watch(name="Zebra", url="https://a.com", content_type="html"))
    db_session.add(Watch(name="Apple", url="https://b.com", content_type="html"))
    await db_session.flush()
    result = await get_watch_list(db_session, sort="name", order="desc")
    assert result[0].name == "Zebra"

async def test_unknown_sort_key_falls_back_to_last_checked(self, db_session):
    # should not raise, just use default sort
    db_session.add(Watch(name="W", url="https://a.com", content_type="html"))
    await db_session.flush()
    result = await get_watch_list(db_session, sort="INVALID", order="asc")
    assert len(result) == 1
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/exedev/watcher
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/dashboard/test_context.py::TestGetWatchList -v 2>&1 | tail -20
```
Expected: `FAILED` on the 5 new tests (TypeError: unexpected keyword argument).

- [ ] **Step 3: Implement the changes in `context.py`**

In `src/dashboard/context.py`, replace the `get_watch_list` function (currently at ~line 40) with:

```python
_WATCH_SORT_COLS: dict[str, Any] = {
    "name": Watch.name,
    "status": Watch.is_active,
    "health": Watch.health_status,
    "last_checked_at": Watch.last_checked_at,
    "last_changed_at": Watch.last_changed_at,
}


async def get_watch_list(
    session: AsyncSession,
    is_active: bool | None = None,
    include_archived: bool = False,
    search: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
) -> list[Watch]:
    """Fetch watches for list display with optional filtering and sorting."""
    col = _WATCH_SORT_COLS.get(sort, Watch.last_checked_at)
    order_expr = col.asc() if order == "asc" else col.desc()
    stmt = select(Watch).order_by(order_expr)
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    if not include_archived:
        stmt = stmt.where(Watch.is_archived.is_(False))
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Watch.name.ilike(f"%{escaped}%"))
    if domain:
        escaped_d = domain.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Watch.effective_domain.ilike(f"%{escaped_d}%"))
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

Add `from typing import Any` to imports at the top of `context.py` if not already present.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/dashboard/test_context.py::TestGetWatchList -v 2>&1 | tail -20
```
Expected: all `TestGetWatchList` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#101 feat: extend get_watch_list with search, domain, sort, order params"
```

---

## Task 2: Extend `get_domain_watches()` with sort, order

**Files:**
- Modify: `src/dashboard/context.py` (`get_domain_watches`, ~line 597)
- Test: `tests/dashboard/test_context.py` (new `TestGetDomainWatches` class)

- [ ] **Step 1: Write failing tests**

Add a new test class at the end of `tests/dashboard/test_context.py`. The class needs `@pytest.mark.integration` (same as all other DB-touching test classes in that file):

```python
@pytest.mark.integration
class TestGetDomainWatches:
    async def test_returns_watches_for_domain(self, db_session):
        db_session.add(Watch(name="W1", url="https://ex.com/a", content_type="html", effective_domain="ex.com"))
        db_session.add(Watch(name="W2", url="https://other.com/b", content_type="html", effective_domain="other.com"))
        await db_session.flush()
        result = await get_domain_watches(db_session, "ex.com")
        assert len(result) == 1
        assert result[0].name == "W1"

    async def test_sort_by_name_asc(self, db_session):
        db_session.add(Watch(name="Zebra", url="https://ex.com/z", content_type="html", effective_domain="ex.com"))
        db_session.add(Watch(name="Apple", url="https://ex.com/a", content_type="html", effective_domain="ex.com"))
        await db_session.flush()
        result = await get_domain_watches(db_session, "ex.com", sort="name", order="asc")
        assert result[0].name == "Apple"

    async def test_sort_by_last_changed_desc(self, db_session):
        from datetime import UTC, datetime
        w1 = Watch(name="Old", url="https://ex.com/old", content_type="html", effective_domain="ex.com",
                   last_changed_at=datetime(2024, 1, 1, tzinfo=UTC))
        w2 = Watch(name="New", url="https://ex.com/new", content_type="html", effective_domain="ex.com",
                   last_changed_at=datetime(2025, 1, 1, tzinfo=UTC))
        db_session.add_all([w1, w2])
        await db_session.flush()
        result = await get_domain_watches(db_session, "ex.com", sort="last_changed_at", order="desc")
        assert result[0].name == "New"
```

Make sure `get_domain_watches` is imported at the top of the test file:
```python
from src.dashboard.context import (
    ...,
    get_domain_watches,
)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/dashboard/test_context.py::TestGetDomainWatches -v 2>&1 | tail -15
```
Expected: `FAILED` (TypeError on `sort`/`order` kwargs).

- [ ] **Step 3: Implement the changes in `context.py`**

Replace `get_domain_watches` (~line 597) with:

```python
async def get_domain_watches(
    session: AsyncSession,
    domain_name: str,
    *,
    search: str | None = None,
    is_active: bool | None = None,
    sort: str = "name",
    order: str = "asc",
) -> list[Watch]:
    """Fetch watches for a domain with optional name search, status filter, and sorting."""
    col = _WATCH_SORT_COLS.get(sort, Watch.name)
    order_expr = col.asc() if order == "asc" else col.desc()
    stmt = select(Watch).where(Watch.effective_domain == domain_name).order_by(order_expr)
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Watch.name.ilike(f"%{escaped}%"))
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run all context tests**

```bash
uv run pytest tests/dashboard/test_context.py -v 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#101 feat: extend get_domain_watches with sort and order params"
```

---

## Task 3: Update route handlers

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_routes.py`

This task:
1. Updates `watches_page` and `partial_watch_table` to accept `q`, `status`, `domain`, `sort`, `order` (replacing `is_active`)
2. Updates `domain_detail_page` to rename `watch_q`→`q`, `watch_status`→`status` and add `sort`, `order`
3. Adds new `partial_domain_watches` route at `GET /partials/domain-watches/{name}`
4. Updates tests that check for old param names

- [ ] **Step 1: Update failing tests first**

In `tests/dashboard/test_routes.py`, update the `TestWatchListFilters` class:

```python
class TestWatchListFilters:
    async def test_watches_page_has_segment_control(self, client):
        response = await client.get("/watches")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="status"' in body  # was: name="is_active"
        assert b'type="radio"' in body

    async def test_watch_table_filter_by_status(self, client):
        response = await client.get("/partials/watch-table?status=active")
        assert response.status_code == 200

    async def test_watch_table_search(self, client):
        response = await client.get("/partials/watch-table?q=something")
        assert response.status_code == 200

    async def test_watch_table_domain_filter(self, client):
        response = await client.get("/partials/watch-table?domain=example.com")
        assert response.status_code == 200

    async def test_watch_table_sort(self, client):
        response = await client.get("/partials/watch-table?sort=name&order=asc")
        assert response.status_code == 200
```

Update the `TestDomainDetailFilters` class:

```python
async def test_domain_detail_has_segment_control(self, client):
    name = await self._create_domain_with_watch(client, "Domain Filter Watch")
    response = await client.get(f"/domains/{name}")
    body = response.content
    assert b'role="radiogroup"' in body
    assert b'name="status"' in body  # was: name="watch_status"

async def test_domain_watches_partial(self, client):
    name = await self._create_domain_with_watch(client, "Partial Watch")
    response = await client.get(f"/partials/domain-watches/{name}")
    assert response.status_code == 200
    assert b"Partial Watch" in response.content

async def test_domain_watches_partial_search(self, client):
    name = await self._create_domain_with_watch(client, "Searchable Watch")
    response = await client.get(f"/partials/domain-watches/{name}?q=searchable")
    assert response.status_code == 200
    assert b"Searchable Watch" in response.content

async def test_domain_watches_partial_sort(self, client):
    name = await self._create_domain_with_watch(client, "Sort Watch")
    response = await client.get(f"/partials/domain-watches/{name}?sort=name&order=asc")
    assert response.status_code == 200
```

Also update the existing `test_watch_table_filter` test inside `TestWatchList` (~line 64). This test currently calls `?is_active=true` — after the route change that param is silently ignored, making the test pass vacuously. Update it explicitly:

```python
# In class TestWatchList:
async def test_watch_table_filter(self, client):
    response = await client.get("/partials/watch-table?status=active")  # was: ?is_active=true
    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/dashboard/test_routes.py::TestWatchListFilters tests/dashboard/test_routes.py::TestDomainDetailFilters -v 2>&1 | tail -25
```
Expected: failures on the changed/new tests.

- [ ] **Step 3: Update `watches_page` and `partial_watch_table` in `routes.py`**

Replace `watches_page` (~line 170):

```python
@router.get("/watches")
async def watches_page(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
    session: AsyncSession = Depends(get_db_session),
):
    """Watch list page."""
    is_active = _status_to_is_active(status)
    watches = await get_watch_list(session, is_active=is_active, search=q, domain=domain, sort=sort, order=order)
    health_map = {w.id: w.health_status for w in watches}
    context = {
        "active_page": "watches",
        "watches": watches,
        "q": q or "",
        "status": status or "",
        "domain": domain or "",
        "sort": sort,
        "order": order,
        "health_map": health_map,
    }
    return templates.TemplateResponse(request, "pages/watches.html", context)
```

Replace `partial_watch_table` (~line 1678):

```python
@router.get("/partials/watch-table")
async def partial_watch_table(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watch table with filter, search, and sort."""
    is_active = _status_to_is_active(status)
    watches = await get_watch_list(session, is_active=is_active, search=q, domain=domain, sort=sort, order=order)
    health_map = {w.id: w.health_status for w in watches}
    return templates.TemplateResponse(
        request,
        "partials/watch_table.html",
        {
            "watches": watches,
            "health_map": health_map,
            "q": q or "",
            "status": status or "",
            "domain": domain or "",
            "sort": sort,
            "order": order,
        },
    )
```

Add the helper `_status_to_is_active` near the top of the route module (after imports):

```python
def _status_to_is_active(status: str | None) -> bool | None:
    if status == "active":
        return True
    if status == "inactive":
        return False
    return None
```

- [ ] **Step 4: Update `domain_detail_page` in `routes.py`**

Replace the `domain_detail_page` route (~line 1353). Change params from `watch_q`/`watch_status` to `q`/`status`, add `sort`/`order`:

```python
@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    is_active = _status_to_is_active(status)
    watches = await get_domain_watches(session, name, search=q, is_active=is_active, sort=sort, order=order)

    field_contexts = {
        fname: _field_context(request, domain, fname, mode="view") for fname in DOMAIN_FIELD_META
    }

    context = {
        "active_page": "domains",
        "domain": domain,
        "watches": watches,
        "q": q or "",
        "status": status or "",
        "sort": sort,
        "order": order,
        "flash": None,
        "field_contexts": field_contexts,
    }
    return templates.TemplateResponse(request, "pages/domain_detail.html", context)
```

- [ ] **Step 5: Add `partial_domain_watches` route**

Add after `partial_watch_table` (around line 1693):

```python
@router.get("/partials/domain-watches/{name}")
async def partial_domain_watches(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domain watch table with filter, search, and sort."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404)
    is_active = _status_to_is_active(status)
    watches = await get_domain_watches(session, name, search=q, is_active=is_active, sort=sort, order=order)
    return templates.TemplateResponse(
        request,
        "partials/domain_watches_table.html",
        {
            "domain": domain,
            "watches": watches,
            "q": q or "",
            "status": status or "",
            "sort": sort,
            "order": order,
        },
    )
```

Make sure `HTTPException` is imported at the top of `routes.py` (check: `from fastapi import ..., HTTPException`).

- [ ] **Step 6: Run route tests**

```bash
uv run pytest tests/dashboard/test_routes.py::TestWatchListFilters tests/dashboard/test_routes.py::TestDomainDetailFilters tests/dashboard/test_routes.py::TestWatchList -v 2>&1 | tail -25
```
Expected: all pass.

- [ ] **Step 7: Run full test suite to check for regressions**

```bash
uv run pytest tests/dashboard/ -v 2>&1 | tail -30
```
Expected: all pass (or only pre-existing failures if any).

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_routes.py
git commit -m "#101 feat: update watch/domain-watches routes with search, sort, domain filter params"
```

---

## Task 4: Create `watch_filters` macro

**Files:**
- Create: `src/dashboard/templates/macros/watch_filters.html`

This macro renders the filter controls area (search input, optional domain input, status radios). It is used by both `watch_table.html` (watches list) and `domain_watches_table.html` (domain detail), passing different `base_url` and `target` values.

- [ ] **Step 1: Verify the vendored HTMX version**

```bash
grep -r "htmx" src/dashboard/static/ --include="*.js" -l
head -1 src/dashboard/static/js/htmx.min.js  # or similar filename
```

Confirm HTMX is 1.x or 2.x. In both versions `hx-vals` takes precedence over `hx-include` when the same key appears in both — the plan relies on this for sort header buttons overriding the hidden sort inputs. If the version is unexpected, check the HTMX changelog before proceeding.

- [ ] **Step 2: Create the macro file**

Create `src/dashboard/templates/macros/watch_filters.html`. The `id` attributes are uniquified using the `target` parameter (e.g. `#watches-container` → `watches-container`) so the macro can be used on the same page without duplicate IDs. Sort state is carried in hidden `name="sort"` / `name="order"` inputs; the sort header buttons send the same param names via `hx-vals`, which HTMX merges on top, so the header values win.

```html
{% macro watch_filters(base_url, target, q="", status="", show_domain=false, domain="", sort="", order="") %}
{% set uid = target|replace('#','') %}
<div class="flex flex-wrap gap-3 items-end mb-4">
  <div class="flex-1 min-w-[200px]">
    <label for="watch-q-{{ uid }}" class="sr-only">Search watches</label>
    <input type="search"
      id="watch-q-{{ uid }}"
      name="q"
      placeholder="Search watches…"
      value="{{ q }}"
      class="form-input text-sm"
      hx-get="{{ base_url }}"
      hx-target="{{ target }}"
      hx-swap="innerHTML"
      hx-trigger="input changed delay:300ms, search"
      hx-include="[name='status'],[name='domain'],[name='sort'],[name='order']"
      aria-label="Search watches by name">
  </div>
  {% if show_domain %}
  <div class="flex-1 min-w-[160px]">
    <label for="watch-domain-{{ uid }}" class="sr-only">Filter by domain</label>
    <input type="search"
      id="watch-domain-{{ uid }}"
      name="domain"
      placeholder="Filter by domain…"
      value="{{ domain }}"
      class="form-input text-sm"
      hx-get="{{ base_url }}"
      hx-target="{{ target }}"
      hx-swap="innerHTML"
      hx-trigger="input changed delay:300ms, search"
      hx-include="[name='q'],[name='status'],[name='sort'],[name='order']"
      aria-label="Filter by domain name">
  </div>
  {% endif %}
  <fieldset class="segment-group" role="radiogroup" aria-label="Filter watches by status">
    {% for value, label in [("", "All"), ("active", "Active"), ("inactive", "Inactive")] %}
    <label class="segment">
      <input type="radio" name="status" value="{{ value }}"
        {% if status == value or (not status and value == '') %}checked{% endif %}
        hx-get="{{ base_url }}"
        hx-target="{{ target }}"
        hx-swap="innerHTML"
        hx-trigger="change"
        hx-include="[name='q'],[name='domain'],[name='sort'],[name='order']">
      <span>{{ label }}</span>
    </label>
    {% endfor %}
  </fieldset>
</div>
{# Hidden inputs carry current sort state so filter interactions preserve it.
   Sort header buttons send the same names via hx-vals, which HTMX merges on
   top of hx-include values — the header's values always win. #}
<input type="hidden" name="sort" value="{{ sort }}">
<input type="hidden" name="order" value="{{ order }}">
{% endmacro %}
```

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/templates/macros/watch_filters.html
git commit -m "#101 feat: add watch_filters Jinja2 macro (search, status, domain)"
```

---

## Task 5: Refactor `watch_table.html` and `watch_row.html`

**Files:**
- Modify: `src/dashboard/templates/partials/watch_table.html`
- Modify: `src/dashboard/templates/partials/watch_row.html`

Removes URL, Type, Deactivate. Adds Last Changed. Adds sortable column headers. Includes the filter macro.

The sortable `<th>` pattern: a `<button>` element with `hx-get`, `hx-target="#watches-container"`, `hx-swap="innerHTML"`, `hx-include="[name='q'],[name='status'],[name='domain']"`, and `hx-vals` to inject `sort`/`order`. The button renders a chevron when that column is currently sorted.

- [ ] **Step 1: Replace `watch_table.html`**

```html
{% from "macros/watch_filters.html" import watch_filters with context %}
{{ watch_filters(
    base_url="/partials/watch-table",
    target="#watches-container",
    q=q,
    status=status,
    show_domain=true,
    domain=domain,
    sort=sort,
    order=order
) }}
{% if watches %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        {% set cols = [
          ("name", "Name"),
          ("status", "Status"),
          ("health", "Health"),
          ("last_checked_at", "Last Checked"),
          ("last_changed_at", "Last Changed"),
        ] %}
        {% for col_key, col_label in cols %}
        {% set is_sorted = sort == col_key %}
        {% set next_order = "asc" if (is_sorted and order == "desc") else "desc" %}
        <th scope="col">
          <button type="button"
            hx-get="/partials/watch-table"
            hx-target="#watches-container"
            hx-swap="innerHTML"
            hx-include="[name='q'],[name='status'],[name='domain']"
            hx-vals='{"sort": "{{ col_key }}", "order": "{{ next_order }}"}'
            class="flex items-center gap-1 text-start w-full font-semibold text-gray-700 dark:text-gray-300 hover:text-co-purple-600 dark:hover:text-co-purple-400 min-h-[44px]"
            aria-label="Sort by {{ col_label }}{% if is_sorted %}, currently {{ order }}ending{% endif %}">
            {{ col_label }}
            <span aria-hidden="true" class="text-xs opacity-60">
              {% if is_sorted %}{{ "↑" if order == "asc" else "↓" }}{% else %}⇅{% endif %}
            </span>
          </button>
        </th>
        {% endfor %}
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

- [ ] **Step 2: Replace `watch_row.html`**

```html
<tr id="watch-{{ watch.id }}" class="hover:bg-gray-50 dark:hover:bg-gray-800">
  <td>
    <a href="/watches/{{ watch.id }}" class="link font-medium">{{ watch.name }}</a>
  </td>
  <td>
    {% if watch.is_active %}
      <span class="badge badge-active">Active</span>
    {% elif watch.domain_suspended %}
      <span class="badge badge-warning">Domain Inactive</span>
    {% else %}
      <span class="badge badge-inactive">Inactive</span>
    {% endif %}
  </td>
  <td>
    {% set health = health_map.get(watch.id, "unknown") %}
    {% if health == "ok" %}
      <span class="badge badge-active">Healthy</span>
    {% elif health == "error" %}
      <span class="badge badge-error">Error</span>
    {% else %}
      <span class="badge badge-inactive">Unknown</span>
    {% endif %}
  </td>
  <td class="text-gray-500 dark:text-gray-400">
    {% if watch.last_checked_at %}{{ watch.last_checked_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}Never{% endif %}
  </td>
  <td class="text-gray-500 dark:text-gray-400">
    {% if watch.last_changed_at %}{{ watch.last_changed_at.strftime('%Y-%m-%d %H:%M UTC') }}{% else %}—{% endif %}
  </td>
</tr>
```

- [ ] **Step 3: Run template-related tests**

```bash
uv run pytest tests/dashboard/test_routes.py -v -k "watch" 2>&1 | tail -30
```

Check for any test that asserts on URL column, Type column, or Deactivate button — update those tests to remove references to the dropped columns and instead assert the new columns exist.

Look for these patterns in `tests/dashboard/test_routes.py` and update:
- Any `assert b"Type" in response.content` or `assert b'content_type' in response.content` (for the table header)
- Any `assert b"URL" in response.content` for the table
- Any assertion about the Deactivate button in the list context

The `deactivate` endpoint itself still exists (used from detail page), so don't remove that test — only remove assertions about the list page showing the button.

- [ ] **Step 4: Run full dashboard test suite**

```bash
uv run pytest tests/dashboard/ -v 2>&1 | tail -30
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/templates/partials/watch_table.html \
        src/dashboard/templates/partials/watch_row.html \
        tests/dashboard/test_routes.py
git commit -m "#101 feat: refactor watch table — remove URL/Type/Deactivate, add Last Changed, sortable headers"
```

---

## Task 6: Update `watches.html` page

**Files:**
- Modify: `src/dashboard/templates/pages/watches.html`

**Ordering note:** This task renames the container from `#watch-table` to `#watches-container`. Task 5's `watch_table.html` already hardcodes `hx-target="#watches-container"`. Tasks 5 and 6 must be committed together or in immediate sequence — if `watches.html` still has `#watch-table` after Task 5 is applied, sort header clicks will silently target a missing element. The commit sequence in this plan is correct; don't reorder them.

**noscript note:** The old `watches.html` wrapped the radios in a `<form>` with a `<noscript>` submit fallback. The new design uses a free-standing search input and HTMX-only radios; a `<noscript>` form submit path is impractical for search+sort+domain together. This is an accepted tradeoff: the filter UI requires JavaScript (consistent with all other HTMX-driven partials in the project).

Remove the old inline radio group. Replace `#watch-table` with `#watches-container` wrapping the included partial. The partial now renders both filter controls and table.

- [ ] **Step 1: Replace `watches.html`**

```html
{% extends "base.html" %}
{% block title %}Watches — Watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6">
  <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Watches</h2>
  <a href="/watches/new" class="btn btn-primary">New Watch</a>
</div>

<div id="watches-container" aria-live="polite" aria-atomic="false">
  {% include "partials/watch_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 2: Verify the page renders**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
sleep 2
curl -s http://localhost:8001/watches | grep -E "watches-container|Search watches|Filter by domain" | head -5
kill %1
```
Expected: output contains `watches-container`, `Search watches`, `Filter by domain`.

- [ ] **Step 3: Run route tests**

```bash
uv run pytest tests/dashboard/test_routes.py::TestWatchList tests/dashboard/test_routes.py::TestWatchListFilters -v 2>&1 | tail -20
```

If any test asserts on `name="is_active"` or the old radio group structure in the full page, update it to match the new structure (radio buttons now come from the macro inside the partial).

- [ ] **Step 4: Commit**

```bash
git add src/dashboard/templates/pages/watches.html tests/dashboard/test_routes.py
git commit -m "#101 feat: update watches page to use filter+table partial in #watches-container"
```

---

## Task 7: Update domain watches table and domain detail page

**Files:**
- Modify: `src/dashboard/templates/partials/domain_watches_table.html`
- Modify: `src/dashboard/templates/pages/domain_detail.html`

The `domain_watches_table.html` partial now includes the filter macro and sortable headers. The `domain_detail.html` page replaces the old inline filter controls with a plain `#domain-watches` container that gets its content from the partial via HTMX.

- [ ] **Step 1: Replace `domain_watches_table.html`**

```html
{% from "macros/watch_filters.html" import watch_filters with context %}
{{ watch_filters(
    base_url="/partials/domain-watches/" ~ domain.name,
    target="#domain-watches",
    q=q,
    status=status,
    show_domain=false,
    sort=sort,
    order=order
) }}
{% if watches %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        {% set cols = [
          ("name", "Name"),
          ("status", "Status"),
          ("last_checked_at", "Last Checked"),
          ("last_changed_at", "Last Changed"),
        ] %}
        {% for col_key, col_label in cols %}
        {% set is_sorted = sort == col_key %}
        {% set next_order = "asc" if (is_sorted and order == "desc") else "desc" %}
        <th scope="col">
          <button type="button"
            hx-get="/partials/domain-watches/{{ domain.name }}"
            hx-target="#domain-watches"
            hx-swap="innerHTML"
            hx-include="[name='q'],[name='status']"
            hx-vals='{"sort": "{{ col_key }}", "order": "{{ next_order }}"}'
            class="flex items-center gap-1 text-start w-full font-semibold text-gray-700 dark:text-gray-300 hover:text-co-purple-600 dark:hover:text-co-purple-400 min-h-[44px]"
            aria-label="Sort by {{ col_label }}{% if is_sorted %}, currently {{ order }}ending{% endif %}">
            {{ col_label }}
            <span aria-hidden="true" class="text-xs opacity-60">
              {% if is_sorted %}{{ "↑" if order == "asc" else "↓" }}{% else %}⇅{% endif %}
            </span>
          </button>
        </th>
        {% endfor %}
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for watch in watches %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td class="font-medium text-gray-900 dark:text-white">
          <a href="/watches/{{ watch.id }}" class="link">{{ watch.name }}</a>
        </td>
        <td>
          {% if watch.is_active %}
            <span class="badge badge-active">Active</span>
          {% elif watch.domain_suspended %}
            <span class="badge badge-warning">Domain Inactive</span>
          {% else %}
            <span class="badge badge-inactive">Inactive</span>
          {% endif %}
        </td>
        <td class="text-gray-500 dark:text-gray-400">
          {{ watch.last_checked_at.strftime("%Y-%m-%d %H:%M") if watch.last_checked_at else "—" }}
        </td>
        <td class="text-gray-500 dark:text-gray-400">
          {{ watch.last_changed_at.strftime("%Y-%m-%d %H:%M") if watch.last_changed_at else "—" }}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm p-4">No watches{% if q or status %} matching filters{% endif %} for this domain.</p>
{% endif %}
```

- [ ] **Step 2: Update the Watches section in `domain_detail.html`**

**Known limitation:** The heading `Watches ({{ watches|length }})` reflects the initial server-rendered count (unfiltered). After a user applies a filter via HTMX, the `#domain-watches` div updates but the heading count does not. This is a pre-existing architectural pattern in the domain detail page (the heading lives outside the swappable container) and is out of scope for this issue. Leave the heading as-is.

Find the Watches section (~line 57 in `domain_detail.html`) and replace it:

**Old:**
```html
{# Watches section #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">
    Watches ({{ watches|length }})
  </h3>

  {% if watches or watch_q or watch_status %}
  <div class="flex flex-wrap gap-3 items-end mb-4">
    ... (entire old filter block) ...
  </div>
  {% endif %}

  <div id="domain-watches" class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
    {% include "partials/domain_watches_table.html" %}
  </div>
</section>
```

**New:**
```html
{# Watches section #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">
    Watches ({{ watches|length }})
  </h3>

  <div id="domain-watches"
    class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden p-4"
    aria-live="polite">
    {% include "partials/domain_watches_table.html" %}
  </div>
</section>
```

The filter controls are now rendered inside the partial, so the section becomes much simpler. The `p-4` padding gives the filter controls some breathing room.

- [ ] **Step 3: Run domain detail tests**

```bash
uv run pytest tests/dashboard/test_routes.py::TestDomainDetailFilters -v 2>&1 | tail -20
```

If any test asserts `b'name="watch_q"'` or `b'name="watch_status"'`, update those — they're now `name="q"` and `name="status"` (inside the macro). Also update any assertion about the filter controls being present on the full page vs. the partial.

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest tests/dashboard/ tests/api/ -v 2>&1 | tail -30
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/templates/partials/domain_watches_table.html \
        src/dashboard/templates/pages/domain_detail.html \
        tests/dashboard/test_routes.py
git commit -m "#101 feat: add sortable headers + Last Changed to domain watches table; move filters into partial"
```

---

## Task 8: Final verification

- [ ] **Step 1: Run linter**

```bash
uv run ruff check src/dashboard/
```
Expected: no errors. Fix any reported issues.

- [ ] **Step 2: Run complete test suite**

```bash
uv run pytest -v 2>&1 | tail -30
```
Expected: all tests pass.

- [ ] **Step 3: Start dev server and manually verify the watches list**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

Visit `https://watcher.exe.xyz:8001/watches` and verify:
- [ ] Search input visible, filters by name with 300ms debounce
- [ ] Domain filter input visible, filters by partial domain match
- [ ] Status radios: All / Active / Inactive
- [ ] Column headers: Name, Status, Health, Last Checked, Last Changed (no URL, no Type)
- [ ] No Deactivate action in rows
- [ ] Clicking column headers toggles asc/desc; chevron appears on sorted column
- [ ] Sort + filter state is preserved when switching between filter controls

- [ ] **Step 4: Verify domain detail watches section**

Visit any domain detail page and verify:
- [ ] Search and status filter controls appear inside the domain watches section
- [ ] Column headers: Name, Status, Last Checked, Last Changed
- [ ] Sorting works; filter + sort state preserved

- [ ] **Step 5: Restart systemd service**

```bash
sudo systemctl restart watcher
```

- [ ] **Step 6: Final commit (if any linter fixes were needed)**

If ruff required fixes, commit them:
```bash
git add -p
git commit -m "#101 fix: ruff lint issues in watch filter routes"
```
