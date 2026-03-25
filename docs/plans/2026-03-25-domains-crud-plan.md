# Domains CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Full CRUD for domains in the dashboard — list with search/filter/pagination, detail with inline-editable fields, create via URL probe, archive/delete lifecycle.

**Architecture:** Add `notes` and `archived_at` columns to the Domain model. Extend the dashboard with list (filter/search/paginate), detail (inline edit, watches sub-table, danger zone), and create (probe-based) views. All mutations are POST with non-HTMX redirect fallback. The existing API layer gets `archived_at`/`notes` support and new archive/restore endpoints.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), Alembic, Jinja2 + HTMX, Tailwind CSS, pytest

**Spec:** `docs/plans/2026-03-25-domains-crud-design.md`

---

## File Structure

### New files
| File | Responsibility |
|---|---|
| `src/dashboard/templates/pages/domain_detail.html` | Detail/edit view template |
| `src/dashboard/templates/pages/domain_form.html` | Create form template |
| `src/dashboard/templates/partials/domain_watches_table.html` | Watches sub-table partial for detail view |
| `src/dashboard/templates/partials/pagination.html` | Reusable pagination partial |
| `tests/dashboard/test_domain_routes.py` | Integration tests for domain dashboard routes |

### Modified files
| File | Changes |
|---|---|
| `src/core/models/domain.py` | Add `notes`, `archived_at` columns; `status` property |
| `src/core/models/audit_log.py` | Add `DOMAIN_*` event type constants |
| `src/api/schemas/domain.py` | Add `notes`, `archived_at` to response; `notes` to patch |
| `src/api/routes/domains.py` | Add `notes`/`archived_at` to upsert; archive/restore endpoints |
| `src/dashboard/routes.py` | Add domain CRUD routes (create, detail, update, archive, restore, delete) |
| `src/dashboard/context.py` | Extend `get_domains_with_watch_counts` with search/filter/pagination/last_checked; add `get_domain_watches` |
| `src/dashboard/templates/pages/domains.html` | Add search/filter bar, "New Domain" button, pagination |
| `src/dashboard/templates/partials/domains_table.html` | New columns (Last Checked, edit button), updated status badges |
| `src/core/config_poller.py` | Exclude archived domains from sync |
| `tests/api/test_domains.py` | Tests for archive/restore, notes field |
| `tests/dashboard/test_context.py` | Tests for search, filter, pagination, last_checked |
| `migrations/versions/` | New migration for `notes` + `archived_at` columns |

---

## Task 1: Domain model — add `notes` and `archived_at` columns

**Files:**
- Modify: `src/core/models/domain.py`
- Test: `tests/api/test_domains.py`

- [ ] **Step 1: Write failing test for `archived_at` field**

In `tests/api/test_domains.py`, add a new test class at the end:

```python
class TestDomainArchiveFields:
    async def test_domain_response_includes_archived_at(self, client):
        response = await client.patch("/api/v1/domains/archive-test.com", json={})
        data = response.json()
        assert "archived_at" in data
        assert data["archived_at"] is None

    async def test_domain_response_includes_notes(self, client):
        response = await client.patch("/api/v1/domains/notes-test.com", json={"notes": "test note"})
        data = response.json()
        assert data["notes"] == "test note"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_domains.py::TestDomainArchiveFields -v`
Expected: FAIL — `archived_at` not in response, `notes` not recognized by schema

- [ ] **Step 3: Add columns to Domain model**

In `src/core/models/domain.py`, add after the `decay_window` column (line 37):

```python
notes: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
archived_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True, default=None
)
```

Add `Text` to the sqlalchemy imports on line 5:

```python
from sqlalchemy import DateTime, Float, Integer, String, Text
```

Add a `status` property after `__init__`:

```python
@property
def status(self) -> str:
    """Derived status: archived > backoff > active."""
    if self.archived_at is not None:
        return "archived"
    if self.current_interval > self.min_interval:
        return "backoff"
    return "active"
```

- [ ] **Step 4: Update schema to include new fields**

In `src/api/schemas/domain.py`:

Add `notes` to `DomainPatch`:
```python
class DomainPatch(BaseModel):
    """Schema for creating or updating a domain config (upsert via PATCH)."""

    min_interval: float | None = Field(None, ge=0)
    max_concurrency: int | None = Field(None, ge=1)
    decay_window: float | None = Field(None, ge=1)
    notes: str | None = None
```

Add `notes` and `archived_at` to `DomainResponse`:
```python
class DomainResponse(BaseModel):
    """Schema for returning a domain config."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    min_interval: float
    max_concurrency: int
    current_interval: float
    last_request_at: datetime | None
    decay_window: float
    notes: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 5: Update upsert route to handle `notes`**

In `src/api/routes/domains.py`, in the `upsert_domain` function:

In the create branch (around line 63), add `notes` to the Domain constructor:
```python
        domain = Domain(
            name=name,
            min_interval=min_iv,
            max_concurrency=updates.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            current_interval=min_iv,
            decay_window=updates.get("decay_window", DEFAULT_DECAY_WINDOW),
            notes=updates.get("notes"),
        )
```

In the update branch (around line 79), add after `decay_window` handling:
```python
        if "notes" in updates:
            domain.notes = updates["notes"]
```

- [ ] **Step 6: Generate Alembic migration**

Run:
```bash
export $(cat env | xargs) && uv run alembic revision --autogenerate -m "add domain notes and archived_at columns"
```

Then apply:
```bash
export $(cat env | xargs) && uv run alembic upgrade head
```

Also apply to test database:
```bash
TEST_DB=$(grep TEST_DATABASE_URL env | cut -d= -f2-) && uv run alembic -x db_url="$TEST_DB" upgrade head
```

Note: The test engine uses `create_all` which creates columns from the model, so tests should work without manually applying the migration to the test DB. But verify.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_domains.py -v`
Expected: All PASS including new `TestDomainArchiveFields` tests

- [ ] **Step 8: Commit**

```bash
git add src/core/models/domain.py src/api/schemas/domain.py src/api/routes/domains.py migrations/versions/ tests/api/test_domains.py
git commit -m "#41 feat: add notes and archived_at columns to Domain model"
```

---

## Task 2: Audit event types for domains

**Files:**
- Modify: `src/core/models/audit_log.py`

- [ ] **Step 1: Add domain event type constants**

In `src/core/models/audit_log.py`, add after line 31 (`PROFILE_DELETED`):

```python
    DOMAIN_CREATED = "domain.created"
    DOMAIN_UPDATED = "domain.updated"
    DOMAIN_ARCHIVED = "domain.archived"
    DOMAIN_RESTORED = "domain.restored"
    DOMAIN_DELETED = "domain.deleted"
```

- [ ] **Step 2: Run existing tests to verify no breakage**

Run: `uv run pytest tests/ -x -q`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/core/models/audit_log.py
git commit -m "#41 feat: add domain audit event types"
```

---

## Task 3: API archive/restore endpoints

**Files:**
- Modify: `src/api/routes/domains.py`
- Test: `tests/api/test_domains.py`

- [ ] **Step 1: Write failing tests for archive/restore**

Add to `tests/api/test_domains.py`:

```python
class TestArchiveDomain:
    async def test_archive_sets_archived_at(self, client):
        await client.patch("/api/v1/domains/arch.com", json={})
        response = await client.post("/api/v1/domains/arch.com/archive")
        assert response.status_code == 200
        assert response.json()["archived_at"] is not None

    async def test_archive_nonexistent_returns_404(self, client):
        response = await client.post("/api/v1/domains/nope.com/archive")
        assert response.status_code == 404

    async def test_archive_already_archived_is_idempotent(self, client):
        await client.patch("/api/v1/domains/idem.com", json={})
        await client.post("/api/v1/domains/idem.com/archive")
        response = await client.post("/api/v1/domains/idem.com/archive")
        assert response.status_code == 200
        assert response.json()["archived_at"] is not None


class TestRestoreDomain:
    async def test_restore_clears_archived_at(self, client):
        await client.patch("/api/v1/domains/rest.com", json={})
        await client.post("/api/v1/domains/rest.com/archive")
        response = await client.post("/api/v1/domains/rest.com/restore")
        assert response.status_code == 200
        assert response.json()["archived_at"] is None

    async def test_restore_nonexistent_returns_404(self, client):
        response = await client.post("/api/v1/domains/nope.com/restore")
        assert response.status_code == 404


class TestDeleteDomainViaApi:
    async def test_delete_domain_still_works_without_archive(self, client):
        """API delete does NOT require archive — only the dashboard enforces that."""
        await client.patch("/api/v1/domains/api-del.com", json={})
        response = await client.delete("/api/v1/domains/api-del.com")
        assert response.status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_domains.py::TestArchiveDomain tests/api/test_domains.py::TestRestoreDomain tests/api/test_domains.py::TestDeleteDomainArchiveGuard -v`
Expected: FAIL — routes don't exist yet

- [ ] **Step 3: Add archive/restore endpoints**

In `src/api/routes/domains.py`, add after `delete_domain`:

```python
from datetime import UTC, datetime


@router.post("/{name}/archive", response_model=DomainResponse)
async def archive_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Archive a domain — excludes it from rate-limiter sync."""
    domain = await _get_domain_or_404(name, session)
    if domain.archived_at is None:
        domain.archived_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(domain)
    return domain


@router.post("/{name}/restore", response_model=DomainResponse)
async def restore_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Restore an archived domain."""
    domain = await _get_domain_or_404(name, session)
    domain.archived_at = None
    await session.commit()
    await session.refresh(domain)
    return domain
```

- [ ] **Step 4: Leave API delete_domain unchanged**

The API `delete_domain` keeps its current behavior (requires zero watches, does NOT require archive). The archive-before-delete guard is enforced only on the dashboard route (Task 11). This avoids a breaking API change for external consumers.

No changes to `delete_domain` or existing delete tests in this step.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_domains.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/api/routes/domains.py tests/api/test_domains.py
git commit -m "#41 feat: add domain archive/restore API endpoints"
```

---

## Task 4: Exclude archived domains from config poller

**Files:**
- Modify: `src/core/config_poller.py`
- Test: `tests/test_config_poller.py` (or existing test file for config poller)

- [ ] **Step 1: Write failing test for archived domain exclusion**

Find or create the config poller test file. Add a test that creates an archived domain, runs `poll_domain_configs`, and asserts the archived domain was NOT synced to the limiter:

```python
async def test_poll_skips_archived_domains(self, db_session):
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from src.core.config_poller import poll_domain_configs
    from src.core.models.domain import Domain

    # Create an archived domain with a recent updated_at
    domain = Domain(name="archived.com", archived_at=datetime.now(UTC))
    db_session.add(domain)
    await db_session.flush()

    limiter = MagicMock()
    last_poll = datetime(2020, 1, 1, tzinfo=UTC)

    # Use a session factory that yields our test session
    # (adjust to match existing test patterns)
    await poll_domain_configs(limiter, session_factory, last_poll)
    limiter.configure_domain.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — archived domain is still synced because query doesn't filter `archived_at`

- [ ] **Step 3: Add archived_at filter to poll query**

In `src/core/config_poller.py`, line 29, change the select statement:

```python
            stmt = select(Domain).where(
                Domain.updated_at > last_poll,
                Domain.archived_at.is_(None),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ -x -q`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/config_poller.py tests/test_config_poller.py
git commit -m "#41 fix: exclude archived domains from config poller sync"
```

---

## Task 5: Context helpers — search, filter, pagination, last_checked

**Files:**
- Modify: `src/dashboard/context.py`
- Test: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing tests for extended `get_domains_with_watch_counts`**

Add to `tests/dashboard/test_context.py`, after the existing `TestGetDomainsWithWatchCounts` class:

```python
from datetime import UTC, datetime


@pytest.mark.integration
class TestGetDomainsFiltered:
    async def test_search_by_name(self, db_session):
        db_session.add(Domain(name="alpha.com"))
        db_session.add(Domain(name="beta.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, search="alpha")
        assert len(result) == 1
        assert result[0]["name"] == "alpha.com"

    async def test_filter_active_excludes_archived(self, db_session):
        db_session.add(Domain(name="active.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, status="active")
        names = [d["name"] for d in result]
        assert "active.com" in names
        assert "gone.com" not in names

    async def test_filter_archived(self, db_session):
        db_session.add(Domain(name="live.com"))
        db_session.add(Domain(name="gone.com", archived_at=datetime.now(UTC)))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, status="archived")
        names = [d["name"] for d in result]
        assert "gone.com" in names
        assert "live.com" not in names

    async def test_filter_backoff(self, db_session):
        db_session.add(Domain(name="normal.com"))
        db_session.add(Domain(name="slow.com", current_interval=5.0))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, status="backoff")
        names = [d["name"] for d in result]
        assert "slow.com" in names
        assert "normal.com" not in names

    async def test_default_filter_is_active(self, db_session):
        db_session.add(Domain(name="visible.com"))
        db_session.add(Domain(name="hidden.com", archived_at=datetime.now(UTC)))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, status="active")
        names = [d["name"] for d in result]
        assert "hidden.com" not in names

    async def test_pagination(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, page=1, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom00.com"

    async def test_pagination_page_2(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, page=2, page_size=2)
        assert len(result) == 2
        assert result[0]["name"] == "dom02.com"

    async def test_total_count_returned(self, db_session):
        for i in range(5):
            db_session.add(Domain(name=f"dom{i:02d}.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session, page=1, page_size=2)
        # Result should have a total_count accessible — we'll return a dict with items + total
        # This test will be adjusted once we decide the return shape
        assert len(result) == 2

    async def test_last_checked_from_watches(self, db_session):
        domain = Domain(name="checked.com")
        db_session.add(domain)
        now = datetime.now(UTC)
        watch = Watch(
            name="W",
            url="https://checked.com",
            content_type="html",
            effective_domain="checked.com",
            last_checked_at=now,
        )
        db_session.add(watch)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["last_checked"] == now

    async def test_last_checked_none_when_no_watches(self, db_session):
        db_session.add(Domain(name="orphan.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["last_checked"] is None

    async def test_result_includes_status(self, db_session):
        db_session.add(Domain(name="s.com"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["status"] == "active"

    async def test_result_includes_notes(self, db_session):
        db_session.add(Domain(name="n.com", notes="important"))
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["notes"] == "important"
```

Add the `datetime` import at top of test file if not present. Add the `get_domains_with_watch_counts` import (already imported).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_context.py::TestGetDomainsFiltered -v`
Expected: FAIL — function doesn't accept new parameters

- [ ] **Step 3: Rewrite `get_domains_with_watch_counts` with search/filter/pagination**

Replace the function in `src/dashboard/context.py` (lines 326-351):

```python
async def get_domains_with_watch_counts(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int | None = None,
) -> list[dict]:
    """Fetch domains with watch count, last_checked, search, filter, and pagination.

    Args:
        search: Substring match on domain name.
        status: Filter — "active", "archived", "backoff", or None (all).
        page: 1-based page number (only used when page_size is set).
        page_size: Results per page. None means no pagination (return all).

    Returns:
        List of domain dicts with keys: name, min_interval, current_interval,
        decay_window, max_concurrency, last_request_at, in_backoff, watch_count,
        last_checked, status, notes, archived_at, id.
    """
    stmt = (
        select(
            Domain,
            func.count(Watch.id).label("watch_count"),
            func.max(Watch.last_checked_at).label("last_checked"),
        )
        .outerjoin(Watch, Watch.effective_domain == Domain.name)
        .group_by(Domain.id)
    )

    if search:
        stmt = stmt.where(Domain.name.ilike(f"%{search}%"))

    if status == "active":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval <= Domain.min_interval,
        )
    elif status == "archived":
        stmt = stmt.where(Domain.archived_at.isnot(None))
    elif status == "backoff":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval > Domain.min_interval,
        )
    # status=None or "all" → no filter

    stmt = stmt.order_by(Domain.name)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "id": str(domain.id),
            "name": domain.name,
            "min_interval": domain.min_interval,
            "current_interval": domain.current_interval,
            "decay_window": domain.decay_window,
            "max_concurrency": domain.max_concurrency,
            "last_request_at": domain.last_request_at,
            "in_backoff": domain.current_interval > domain.min_interval,
            "watch_count": watch_count,
            "last_checked": last_checked,
            "status": domain.status,
            "notes": domain.notes,
            "archived_at": domain.archived_at,
        }
        for domain, watch_count, last_checked in rows
    ]
```

- [ ] **Step 4: Add a count helper for pagination metadata**

Add below the function:

```python
async def get_domains_total_count(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
) -> int:
    """Count total domains matching search/filter (for pagination)."""
    stmt = select(func.count(Domain.id))

    if search:
        stmt = stmt.where(Domain.name.ilike(f"%{search}%"))

    if status == "active":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval <= Domain.min_interval,
        )
    elif status == "archived":
        stmt = stmt.where(Domain.archived_at.isnot(None))
    elif status == "backoff":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval > Domain.min_interval,
        )

    result = await session.execute(stmt)
    return result.scalar_one()
```

- [ ] **Step 5: Add `get_domain_watches` helper for detail view**

Add below:

```python
async def get_domain_watches(
    session: AsyncSession,
    domain_name: str,
    *,
    search: str | None = None,
    is_active: bool | None = None,
) -> list[Watch]:
    """Fetch watches for a domain with optional name search and status filter."""
    stmt = select(Watch).where(Watch.effective_domain == domain_name)

    if search:
        stmt = stmt.where(Watch.name.ilike(f"%{search}%"))
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)

    stmt = stmt.order_by(Watch.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_context.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#41 feat: add search, filter, pagination to domain context helpers"
```

---

## Task 6: Pagination partial template

**Files:**
- Create: `src/dashboard/templates/partials/pagination.html`

- [ ] **Step 1: Create reusable pagination partial**

Create `src/dashboard/templates/partials/pagination.html`:

```html
{# Reusable sticky-footer pagination. Expects: page, page_size, total_count, base_url, extra_params (dict). #}
{% set total_pages = (total_count + page_size - 1) // page_size %}
{% if total_pages > 1 or page_size != 25 %}
<nav class="sticky bottom-0 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-700 py-3 px-4 flex items-center justify-between mt-4" aria-label="Pagination">
  <div class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
    <label for="page-size" class="sr-only">Page size</label>
    <select id="page-size"
      class="form-input py-1 px-2 text-sm w-auto"
      hx-get="{{ base_url }}"
      hx-target="{{ hx_target|default('#domains-table-container') }}"
      hx-swap="innerHTML"
      hx-include="[name='q'],[name='status']"
      name="page_size"
      aria-label="Results per page">
      {% for size in [25, 50, 100] %}
      <option value="{{ size }}" {% if page_size == size %}selected{% endif %}>{{ size }}</option>
      {% endfor %}
    </select>
    <span>of {{ total_count }} results</span>
  </div>

  <div class="flex items-center gap-1">
    {% if page > 1 %}
    <a href="{{ base_url }}?page={{ page - 1 }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
       hx-get="{{ base_url }}?page={{ page - 1 }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
       hx-target="{{ hx_target|default('#domains-table-container') }}"
       hx-swap="innerHTML"
       class="btn btn-secondary py-1 px-2 text-sm min-h-[44px]">Prev</a>
    {% endif %}

    {% for p in range(1, total_pages + 1) %}
      {% if p == page %}
      <span class="btn btn-primary py-1 px-2 text-sm min-h-[44px]" aria-current="page">{{ p }}</span>
      {% elif p <= 3 or p > total_pages - 2 or (p >= page - 1 and p <= page + 1) %}
      <a href="{{ base_url }}?page={{ p }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
         hx-get="{{ base_url }}?page={{ p }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
         hx-target="{{ hx_target|default('#domains-table-container') }}"
         hx-swap="innerHTML"
         class="btn btn-secondary py-1 px-2 text-sm min-h-[44px]">{{ p }}</a>
      {% elif p == 4 or p == total_pages - 2 %}
      <span class="px-1 text-gray-400">…</span>
      {% endif %}
    {% endfor %}

    {% if page < total_pages %}
    <a href="{{ base_url }}?page={{ page + 1 }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
       hx-get="{{ base_url }}?page={{ page + 1 }}&page_size={{ page_size }}{% for k, v in extra_params.items() %}&{{ k }}={{ v }}{% endfor %}"
       hx-target="{{ hx_target|default('#domains-table-container') }}"
       hx-swap="innerHTML"
       class="btn btn-secondary py-1 px-2 text-sm min-h-[44px]">Next</a>
    {% endif %}
  </div>
</nav>
{% endif %}
```

- [ ] **Step 2: Commit**

```bash
git add src/dashboard/templates/partials/pagination.html
git commit -m "#41 feat: add reusable pagination partial template"
```

---

## Task 7: List view — updated page template and table partial

**Files:**
- Modify: `src/dashboard/templates/pages/domains.html`
- Modify: `src/dashboard/templates/partials/domains_table.html`
- Modify: `src/dashboard/routes.py`

- [ ] **Step 1: Write failing test for list view search and filter**

Add to `tests/dashboard/test_domain_routes.py` (new file):

```python
"""Integration tests for domain dashboard routes."""

import pytest

from src.core.models.domain import Domain

pytestmark = pytest.mark.integration


class TestDomainsListPage:
    async def test_domains_page_returns_200(self, client):
        response = await client.get("/domains")
        assert response.status_code == 200
        assert b"Domains" in response.content

    async def test_domains_page_has_create_link(self, client):
        response = await client.get("/domains")
        assert b"/domains/new" in response.content

    async def test_domains_page_has_search_input(self, client):
        response = await client.get("/domains")
        assert b'name="q"' in response.content

    async def test_domains_page_has_filter_pills(self, client):
        response = await client.get("/domains")
        assert b"Active" in response.content
        assert b"Archived" in response.content

    async def test_domains_table_partial(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200

    async def test_domains_table_search(self, client, db_session):
        db_session.add(Domain(name="findme.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()

        response = await client.get("/partials/domains-table?q=findme")
        assert response.status_code == 200
        assert b"findme.com" in response.content
        assert b"other.com" not in response.content

    async def test_domains_table_has_edit_button(self, client, db_session):
        db_session.add(Domain(name="editable.com"))
        await db_session.flush()

        response = await client.get("/partials/domains-table")
        assert b"/domains/editable.com" in response.content

    async def test_domains_table_shows_last_checked(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200
        # Column header present
        assert b"Last Checked" in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainsListPage -v`
Expected: FAIL — search input not present, filter pills not present

- [ ] **Step 3: Update domains page template**

Replace `src/dashboard/templates/pages/domains.html`:

```html
{% extends "base.html" %}
{% block title %}Domains — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6 flex-wrap gap-4">
  <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Domains</h2>
  <a href="/domains/new" class="btn btn-primary min-h-[44px]">New Domain</a>
</div>

{# Search and filter bar #}
<div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 mb-6">
  <div class="flex flex-wrap gap-3 items-end">
    <div class="flex-1 min-w-[200px]">
      <label for="domain-search" class="sr-only">Search domains</label>
      <input type="search" id="domain-search" name="q"
        placeholder="Search domains…"
        value="{{ search or '' }}"
        class="form-input"
        hx-get="/partials/domains-table"
        hx-target="{{ hx_target|default('#domains-table-container') }}"
        hx-swap="innerHTML"
        hx-trigger="input changed delay:300ms, search"
        hx-include="[name='status'],[name='page_size']"
        aria-label="Search domains by name">
    </div>
    <div class="flex gap-1" role="group" aria-label="Filter by status">
      {% for value, label in [("", "All"), ("active", "Active"), ("archived", "Archived"), ("backoff", "Backoff")] %}
      <button
        class="filter-pill {% if status == value or (not status and value == 'active') %}filter-pill-active{% endif %} min-h-[44px]"
        name="status"
        value="{{ value }}"
        hx-get="/partials/domains-table"
        hx-target="{{ hx_target|default('#domains-table-container') }}"
        hx-swap="innerHTML"
        hx-include="[name='q'],[name='page_size']">
        {{ label }}
      </button>
      {% endfor %}
    </div>
  </div>
</div>

<div id="domains-table-container"
     aria-live="polite" aria-atomic="false">
  {% include "partials/domains_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 4: Update domains table partial**

Replace `src/dashboard/templates/partials/domains_table.html`:

```html
{% if domains %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Domain</th>
        <th>Status</th>
        <th>Watches</th>
        <th>Last Checked</th>
        <th><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for domain in domains %}
      <tr class="{% if domain.status == 'archived' %}opacity-60{% elif domain.in_backoff %}bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/30{% else %}hover:bg-gray-50 dark:hover:bg-gray-800{% endif %}">
        <td class="font-medium text-gray-900 dark:text-white">{{ domain.name }}</td>
        <td>
          {% if domain.status == "archived" %}
          <span class="badge badge-inactive">Archived</span>
          {% elif domain.status == "backoff" %}
          <span class="badge badge-warning">Backoff</span>
          {% else %}
          <span class="badge badge-active">Active</span>
          {% endif %}
        </td>
        <td>{{ domain.watch_count }}</td>
        <td>{{ domain.last_checked.strftime("%Y-%m-%d %H:%M") if domain.last_checked else "—" }}</td>
        <td class="text-end">
          <a href="/domains/{{ domain.name }}" class="btn btn-secondary py-1 px-3 text-sm min-h-[44px]">Edit</a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% include "partials/pagination.html" %}
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No domains found.</p>
{% endif %}
```

- [ ] **Step 5: Update dashboard routes to pass search/filter/pagination params**

In `src/dashboard/routes.py`, update the `domains_page` route:

```python
@router.get("/domains")
async def domains_page(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """Domains list page with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session, search=q, status=status, page=page, page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    context = {
        "request": request,
        "active_page": "domains",
        "domains": domains,
        "search": q,
        "status": status,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "base_url": "/partials/domains-table",
        "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
    }
    return templates.TemplateResponse("pages/domains.html", context)
```

Update the `partial_domains_table` route similarly:

```python
@router.get("/partials/domains-table")
async def partial_domains_table(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domains table with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session, search=q, status=status, page=page, page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    return templates.TemplateResponse(
        "partials/domains_table.html",
        {
            "request": request,
            "domains": domains,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/domains-table",
            "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
        },
    )
```

Add `get_domains_total_count` to the imports from `src.dashboard.context`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainsListPage tests/dashboard/test_routes.py -v`
Expected: All PASS (including existing tests that call the old route signature)

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/pages/domains.html src/dashboard/templates/partials/domains_table.html src/dashboard/routes.py
git commit -m "#41 feat: domain list view with search, filter, pagination"
```

---

## Task 8: Create view — probe-based domain creation

**Files:**
- Modify: `src/dashboard/routes.py`
- Create: `src/dashboard/templates/pages/domain_form.html`
- Test: `tests/dashboard/test_domain_routes.py`

- [ ] **Step 1: Write failing tests for create flow**

Add to `tests/dashboard/test_domain_routes.py`:

```python
class TestDomainCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/domains/new")
        assert response.status_code == 200
        assert b"New Domain" in response.content

    async def test_create_form_has_url_input(self, client):
        response = await client.get("/domains/new")
        assert b'name="url"' in response.content

    async def test_create_domain_redirects_to_detail(self, client):
        response = await client.post(
            "/domains",
            data={"url": "https://newdomain.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "newdomain.com" in response.headers["location"]

    async def test_create_domain_missing_url_shows_error(self, client):
        response = await client.post("/domains", data={"url": ""})
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()

    async def test_create_domain_duplicate_redirects_to_existing(self, client, db_session):
        db_session.add(Domain(name="existing.com"))
        await db_session.flush()

        response = await client.post(
            "/domains",
            data={"url": "https://existing.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "existing.com" in response.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainCreate -v`
Expected: FAIL — routes don't exist

- [ ] **Step 3: Create domain form template**

Create `src/dashboard/templates/pages/domain_form.html`:

```html
{% extends "base.html" %}
{% block title %}New Domain — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">New Domain</h2>

{% include "partials/flash.html" %}

<form method="post" action="/domains" class="max-w-xl space-y-6">
  <div>
    <label for="url" class="form-label">URL</label>
    <input type="url" name="url" id="url" required
      value="{{ url or '' }}"
      placeholder="https://example.com/page"
      aria-describedby="url-hint"
      class="form-input mt-1">
    <p id="url-hint" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
      Enter any URL — the domain will be extracted automatically.
    </p>
    <p id="url-error" class="mt-1 text-xs text-red-600 dark:text-red-400" hidden></p>
  </div>

  <div class="flex gap-3">
    <button type="submit" class="btn btn-primary min-h-[44px]">Create Domain</button>
    <a href="/domains" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Add create routes to dashboard**

In `src/dashboard/routes.py`, add the create routes. Place them BEFORE the `domains/{name}` route to avoid path conflicts:

Add these to the top-level imports in `src/dashboard/routes.py`:

```python
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select

from src.api.dependencies import get_probe_fn
from src.core.models.domain import Domain
from src.core.probe import ProbeResult
```

Also add to the `from src.dashboard.context import` block:
```python
from src.dashboard.context import (
    ...existing imports...,
    get_domain_watches,
    get_domains_total_count,
)
```

Then add the routes:

```python


@router.get("/domains/new")
async def domain_create_form(request: Request):
    """Domain creation form."""
    return templates.TemplateResponse(
        "pages/domain_form.html",
        {"request": request, "active_page": "domains", "flash": None, "url": ""},
    )


@router.post("/domains")
async def domain_create_submit(
    request: Request,
    url: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """Create domain by probing a URL to extract the effective domain."""
    if not url.strip():
        flash = {"type": "error", "message": "URL is required"}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    try:
        result = await probe_fn(url.strip())
    except Exception:
        flash = {"type": "error", "message": "Could not reach URL. Check the address and try again."}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    domain_name = result.effective_domain
    if not domain_name:
        flash = {"type": "error", "message": "Could not extract domain from URL."}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    # Check if domain already exists

    existing = await session.execute(
        select(Domain).where(Domain.name == domain_name)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)

    domain = Domain(name=domain_name)
    session.add(domain)
    audit(session, EventType.DOMAIN_CREATED, domain_name=domain_name, source="dashboard")
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)
```

**Import note:** Add `select` to `routes.py` file-level imports: `from sqlalchemy import select`. Also add `Domain` import: `from src.core.models.domain import Domain`. No inline imports — project conventions require all imports at file top.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainCreate -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/domain_form.html tests/dashboard/test_domain_routes.py
git commit -m "#41 feat: probe-based domain create view"
```

---

## Task 9: Detail view — inline-editable fields, watches, metadata, danger zone

**Files:**
- Modify: `src/dashboard/routes.py`
- Create: `src/dashboard/templates/pages/domain_detail.html`
- Create: `src/dashboard/templates/partials/domain_watches_table.html`
- Test: `tests/dashboard/test_domain_routes.py`

- [ ] **Step 1: Write failing tests for detail view**

Add to `tests/dashboard/test_domain_routes.py`:

```python
class TestDomainDetail:
    async def test_detail_page_returns_200(self, client, db_session):
        db_session.add(Domain(name="detail.com"))
        await db_session.flush()

        response = await client.get("/domains/detail.com")
        assert response.status_code == 200
        assert b"detail.com" in response.content

    async def test_detail_page_404_nonexistent(self, client):
        response = await client.get("/domains/nonexistent.com")
        assert response.status_code == 404

    async def test_detail_page_shows_config_fields(self, client, db_session):
        db_session.add(Domain(name="config.com", min_interval=3.5, max_concurrency=5))
        await db_session.flush()

        response = await client.get("/domains/config.com")
        assert b"3.5" in response.content
        assert b"Minimum seconds between requests" in response.content

    async def test_detail_page_shows_notes(self, client, db_session):
        db_session.add(Domain(name="noted.com", notes="Important note"))
        await db_session.flush()

        response = await client.get("/domains/noted.com")
        assert b"Important note" in response.content

    async def test_detail_page_shows_watches_section(self, client, db_session):
        from src.core.models.watch import Watch

        db_session.add(Domain(name="watched.com"))
        db_session.add(Watch(
            name="My Watch", url="https://watched.com/page",
            content_type="html", effective_domain="watched.com",
        ))
        await db_session.flush()

        response = await client.get("/domains/watched.com")
        assert b"Watches" in response.content
        assert b"My Watch" in response.content

    async def test_detail_page_shows_metadata(self, client, db_session):
        db_session.add(Domain(name="meta.com"))
        await db_session.flush()

        response = await client.get("/domains/meta.com")
        assert b"Metadata" in response.content

    async def test_detail_page_shows_danger_zone(self, client, db_session):
        db_session.add(Domain(name="danger.com"))
        await db_session.flush()

        response = await client.get("/domains/danger.com")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainDetail -v`
Expected: FAIL — route returns 404 or doesn't exist

- [ ] **Step 3: Add detail route**

In `src/dashboard/routes.py`, add AFTER the create routes and AFTER any `/domains/new` route:

```python
from src.dashboard.context import get_domain_watches


@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    watch_q: str | None = None,
    watch_status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""


    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    is_active = None
    if watch_status == "active":
        is_active = True
    elif watch_status == "inactive":
        is_active = False

    watches = await get_domain_watches(session, name, search=watch_q, is_active=is_active)

    context = {
        "request": request,
        "active_page": "domains",
        "domain": domain,
        "watches": watches,
        "watch_q": watch_q,
        "watch_status": watch_status,
        "flash": None,
    }
    return templates.TemplateResponse("pages/domain_detail.html", context)
```

- [ ] **Step 4: Create detail page template**

Create `src/dashboard/templates/pages/domain_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ domain.name }} — watcher{% endblock %}
{% block content %}
<div class="mb-6">
  <div class="flex items-center gap-3 flex-wrap">
    <h2 class="text-2xl font-bold text-gray-900 dark:text-white">{{ domain.name }}</h2>
    {% if domain.status == "archived" %}
    <span class="badge badge-inactive">Archived</span>
    {% elif domain.status == "backoff" %}
    <span class="badge badge-warning">Backoff</span>
    {% else %}
    <span class="badge badge-active">Active</span>
    {% endif %}
  </div>
</div>

{% include "partials/flash.html" %}

{# Details — inline-editable fields #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Details</h3>
  <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 divide-y divide-gray-200 dark:divide-gray-700">

    {# min_interval #}
    <div class="p-4" id="field-min_interval">
      <form method="post" action="/domains/{{ domain.name }}" class="flex flex-col sm:flex-row sm:items-center gap-2">
        <input type="hidden" name="field" value="min_interval">
        <div class="flex-1">
          <label for="min_interval" class="form-label mb-0">Min Interval</label>
          <p class="text-xs text-gray-500 dark:text-gray-400">Minimum seconds between requests to this domain</p>
        </div>
        <div class="flex items-center gap-2">
          <input type="number" name="value" id="min_interval" step="0.1" min="0.1"
            value="{{ '%.1f'|format(domain.min_interval) }}"
            class="form-input w-28 text-sm"
            hx-post="/domains/{{ domain.name }}"
            hx-target="#field-min_interval"
            hx-swap="outerHTML"
            hx-vals='{"field": "min_interval"}'
            hx-trigger="change">
          <span class="text-sm text-gray-500 dark:text-gray-400">seconds</span>
          <noscript><button type="submit" class="btn btn-secondary py-1 px-2 text-sm">Save</button></noscript>
        </div>
      </form>
    </div>

    {# max_concurrency #}
    <div class="p-4" id="field-max_concurrency">
      <form method="post" action="/domains/{{ domain.name }}" class="flex flex-col sm:flex-row sm:items-center gap-2">
        <input type="hidden" name="field" value="max_concurrency">
        <div class="flex-1">
          <label for="max_concurrency" class="form-label mb-0">Max Concurrency</label>
          <p class="text-xs text-gray-500 dark:text-gray-400">Maximum simultaneous requests allowed</p>
        </div>
        <div class="flex items-center gap-2">
          <input type="number" name="value" id="max_concurrency" min="1"
            value="{{ domain.max_concurrency }}"
            class="form-input w-28 text-sm"
            hx-post="/domains/{{ domain.name }}"
            hx-target="#field-max_concurrency"
            hx-swap="outerHTML"
            hx-vals='{"field": "max_concurrency"}'
            hx-trigger="change">
          <noscript><button type="submit" class="btn btn-secondary py-1 px-2 text-sm">Save</button></noscript>
        </div>
      </form>
    </div>

    {# decay_window #}
    <div class="p-4" id="field-decay_window">
      <form method="post" action="/domains/{{ domain.name }}" class="flex flex-col sm:flex-row sm:items-center gap-2">
        <input type="hidden" name="field" value="decay_window">
        <div class="flex-1">
          <label for="decay_window" class="form-label mb-0">Decay Window</label>
          <p class="text-xs text-gray-500 dark:text-gray-400">Seconds before backoff interval decays toward minimum</p>
        </div>
        <div class="flex items-center gap-2">
          <input type="number" name="value" id="decay_window" step="1" min="1"
            value="{{ '%.0f'|format(domain.decay_window) }}"
            class="form-input w-28 text-sm"
            hx-post="/domains/{{ domain.name }}"
            hx-target="#field-decay_window"
            hx-swap="outerHTML"
            hx-vals='{"field": "decay_window"}'
            hx-trigger="change">
          <span class="text-sm text-gray-500 dark:text-gray-400">seconds</span>
          <noscript><button type="submit" class="btn btn-secondary py-1 px-2 text-sm">Save</button></noscript>
        </div>
      </form>
    </div>

    {# notes #}
    <div class="p-4" id="field-notes">
      <form method="post" action="/domains/{{ domain.name }}" class="flex flex-col gap-2">
        <input type="hidden" name="field" value="notes">
        <label for="notes" class="form-label mb-0">Notes</label>
        <textarea name="value" id="notes" rows="3"
          class="form-input text-sm"
          hx-post="/domains/{{ domain.name }}"
          hx-target="#field-notes"
          hx-swap="outerHTML"
          hx-vals='{"field": "notes"}'
          hx-trigger="change"
          placeholder="Operator notes about this domain…">{{ domain.notes or '' }}</textarea>
        <noscript><button type="submit" class="btn btn-secondary py-1 px-2 text-sm self-start">Save</button></noscript>
      </form>
    </div>

  </div>
</section>

{# Watches section #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">
    Watches ({{ watches|length }})
  </h3>

  {% if watches or watch_q or watch_status %}
  <div class="flex flex-wrap gap-3 items-end mb-4">
    <div class="flex-1 min-w-[200px]">
      <label for="watch-search" class="sr-only">Search watches</label>
      <input type="search" id="watch-search" name="watch_q"
        placeholder="Search watches…"
        value="{{ watch_q or '' }}"
        class="form-input text-sm"
        hx-get="/domains/{{ domain.name }}"
        hx-target="#domain-watches"
        hx-select="#domain-watches"
        hx-swap="outerHTML"
        hx-trigger="input changed delay:300ms, search"
        hx-include="[name='watch_status']"
        aria-label="Search watches by name">
    </div>
    <div class="flex gap-1" role="group" aria-label="Filter watches by status">
      {% for value, label in [("", "All"), ("active", "Active"), ("inactive", "Inactive")] %}
      <button
        class="filter-pill {% if watch_status == value or (not watch_status and value == '') %}filter-pill-active{% endif %} min-h-[44px]"
        name="watch_status"
        value="{{ value }}"
        hx-get="/domains/{{ domain.name }}"
        hx-target="#domain-watches"
        hx-select="#domain-watches"
        hx-swap="outerHTML"
        hx-include="[name='watch_q']">
        {{ label }}
      </button>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div id="domain-watches">
    {% include "partials/domain_watches_table.html" %}
  </div>
</section>

{# Metadata #}
<p class="text-xs text-gray-400 dark:text-gray-500 mb-8">
  Metadata · ID: {{ domain.id }} · Created: {{ domain.created_at.strftime("%Y-%m-%d") }} · Updated: {{ domain.updated_at.strftime("%Y-%m-%d") }}
</p>

{# Danger Zone #}
<section class="border border-red-200 dark:border-red-800 rounded-lg p-6">
  <h3 class="text-lg font-semibold text-red-600 dark:text-red-400 mb-4">Danger Zone</h3>
  <div id="danger-zone-error"></div>

  {% if domain.archived_at is none %}
  <div class="flex items-center justify-between">
    <div>
      <p class="text-sm font-medium text-gray-900 dark:text-white">Archive this domain</p>
      <p class="text-xs text-gray-500 dark:text-gray-400">Excludes from rate-limiter sync. Can be restored.</p>
    </div>
    <button
      hx-post="/domains/{{ domain.name }}/archive"
      hx-confirm="Archive {{ domain.name }}? It will be excluded from rate limiting."
      class="btn btn-danger-outline min-h-[44px]">
      Archive
    </button>
  </div>
  {% else %}
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <p class="text-sm font-medium text-gray-900 dark:text-white">Restore this domain</p>
        <p class="text-xs text-gray-500 dark:text-gray-400">Re-enable rate-limiter sync.</p>
      </div>
      <button
        hx-post="/domains/{{ domain.name }}/restore"
        class="btn btn-secondary min-h-[44px]">
        Restore
      </button>
    </div>

    <hr class="border-red-200 dark:border-red-800">

    <div class="flex items-center justify-between">
      <div>
        <p class="text-sm font-medium text-gray-900 dark:text-white">Delete this domain</p>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          {% if watches|length > 0 %}
          Cannot delete — {{ watches|length }} watch{{ 'es' if watches|length != 1 }} still reference this domain.
          {% else %}
          Permanently remove this domain. This cannot be undone.
          {% endif %}
        </p>
      </div>
      <button
        {% if watches|length == 0 %}
        hx-post="/domains/{{ domain.name }}/delete"
        hx-target="#danger-zone-error"
        hx-swap="innerHTML"
        hx-confirm="Permanently delete {{ domain.name }}? This cannot be undone."
        class="btn btn-danger min-h-[44px]"
        {% else %}
        disabled
        class="btn btn-danger min-h-[44px] opacity-50 cursor-not-allowed"
        {% endif %}>
        Delete
      </button>
    </div>
  </div>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 5: Create domain watches sub-table partial**

Create `src/dashboard/templates/partials/domain_watches_table.html`:

```html
{% if watches %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Name</th>
        <th>Status</th>
        <th>Last Checked</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
      {% for watch in watches %}
      <tr class="hover:bg-gray-50 dark:hover:bg-gray-800">
        <td class="font-medium text-gray-900 dark:text-white">
          <a href="/watches/{{ watch.id }}" class="link">{{ watch.name }}</a>
        </td>
        <td>
          <span class="badge {% if watch.is_active %}badge-active{% else %}badge-inactive{% endif %}">
            {{ "Active" if watch.is_active else "Inactive" }}
          </span>
        </td>
        <td>{{ watch.last_checked_at.strftime("%Y-%m-%d %H:%M") if watch.last_checked_at else "—" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 dark:text-gray-400 text-sm">No watches using this domain.</p>
{% endif %}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainDetail -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/domain_detail.html src/dashboard/templates/partials/domain_watches_table.html tests/dashboard/test_domain_routes.py
git commit -m "#41 feat: domain detail view with inline edit, watches, danger zone"
```

---

## Task 10: Inline field update route

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_domain_routes.py`

- [ ] **Step 1: Write failing tests for inline field update**

Add to `tests/dashboard/test_domain_routes.py`:

```python
class TestDomainInlineUpdate:
    async def test_update_min_interval(self, client, db_session):
        db_session.add(Domain(name="update.com"))
        await db_session.flush()

        response = await client.post(
            "/domains/update.com",
            data={"field": "min_interval", "value": "5.0"},
        )
        assert response.status_code == 200

    async def test_update_notes(self, client, db_session):
        db_session.add(Domain(name="notes-update.com"))
        await db_session.flush()

        response = await client.post(
            "/domains/notes-update.com",
            data={"field": "notes", "value": "Updated note"},
        )
        assert response.status_code == 200
        assert b"Updated note" in response.content

    async def test_update_invalid_field_returns_400(self, client, db_session):
        db_session.add(Domain(name="bad-field.com"))
        await db_session.flush()

        response = await client.post(
            "/domains/bad-field.com",
            data={"field": "name", "value": "hacked"},
        )
        assert response.status_code == 400

    async def test_update_nonexistent_returns_404(self, client):
        response = await client.post(
            "/domains/nope.com",
            data={"field": "min_interval", "value": "5.0"},
        )
        assert response.status_code == 404

    async def test_update_non_htmx_redirects(self, client, db_session):
        db_session.add(Domain(name="redirect.com"))
        await db_session.flush()

        response = await client.post(
            "/domains/redirect.com",
            data={"field": "min_interval", "value": "5.0"},
            follow_redirects=False,
        )
        # Non-HTMX should either return the partial or redirect
        assert response.status_code in (200, 303)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainInlineUpdate -v`
Expected: FAIL — route doesn't exist

- [ ] **Step 3: Add inline update route**

In `src/dashboard/routes.py`:

```python
EDITABLE_DOMAIN_FIELDS = {"min_interval", "max_concurrency", "decay_window", "notes"}
DOMAIN_FIELD_TYPES = {
    "min_interval": float,
    "max_concurrency": int,
    "decay_window": float,
    "notes": str,
}


@router.post("/domains/{name}")
async def domain_inline_update(
    request: Request,
    name: str,
    field: str = Form(""),
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single domain field (inline edit from detail view)."""


    if field not in EDITABLE_DOMAIN_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field}' is not editable")

    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Cast value to correct type
    cast_fn = DOMAIN_FIELD_TYPES[field]
    try:
        typed_value = cast_fn(value) if field != "notes" else value
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid value for {field}")

    setattr(domain, field, typed_value)
    audit(session, EventType.DOMAIN_UPDATED, domain_name=name, field=field, source="dashboard")
    await session.commit()
    await session.refresh(domain)

    # Return the updated field partial for HTMX swap
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        # Re-render just the detail page (HTMX will select the target)
        watches = await get_domain_watches(session, name)
        return templates.TemplateResponse(
            "pages/domain_detail.html",
            {
                "request": request,
                "active_page": "domains",
                "domain": domain,
                "watches": watches,
                "watch_q": None,
                "watch_status": None,
                "flash": None,
            },
        )
    return RedirectResponse(url=f"/domains/{name}", status_code=303)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainInlineUpdate -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_domain_routes.py
git commit -m "#41 feat: domain inline field update route"
```

---

## Task 11: Dashboard archive/restore/delete routes

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_domain_routes.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/dashboard/test_domain_routes.py`:

```python
from datetime import UTC, datetime


class TestDomainArchive:
    async def test_archive_domain(self, client, db_session):
        db_session.add(Domain(name="to-archive.com"))
        await db_session.flush()

        response = await client.post("/domains/to-archive.com/archive", follow_redirects=False)
        assert response.status_code == 303

    async def test_archive_nonexistent_returns_404(self, client):
        response = await client.post("/domains/nope.com/archive")
        assert response.status_code == 404


class TestDomainRestore:
    async def test_restore_domain(self, client, db_session):
        db_session.add(Domain(name="to-restore.com", archived_at=datetime.now(UTC)))
        await db_session.flush()

        response = await client.post("/domains/to-restore.com/restore", follow_redirects=False)
        assert response.status_code == 303


class TestDomainDelete:
    async def test_delete_archived_domain(self, client, db_session):
        db_session.add(Domain(name="to-delete.com", archived_at=datetime.now(UTC)))
        await db_session.flush()

        response = await client.post("/domains/to-delete.com/delete", follow_redirects=False)
        assert response.status_code == 303

    async def test_delete_active_domain_returns_409(self, client, db_session):
        db_session.add(Domain(name="no-delete.com"))
        await db_session.flush()

        response = await client.post("/domains/no-delete.com/delete")
        assert response.status_code == 409

    async def test_delete_domain_with_watches_returns_409(self, client, db_session):
        from src.core.models.watch import Watch

        db_session.add(Domain(name="busy-del.com", archived_at=datetime.now(UTC)))
        db_session.add(Watch(
            name="W", url="https://busy-del.com/p",
            content_type="html", effective_domain="busy-del.com",
        ))
        await db_session.flush()

        response = await client.post("/domains/busy-del.com/delete")
        assert response.status_code == 409

    async def test_delete_nonexistent_returns_404(self, client):
        response = await client.post("/domains/nope.com/delete")
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_domain_routes.py::TestDomainArchive tests/dashboard/test_domain_routes.py::TestDomainRestore tests/dashboard/test_domain_routes.py::TestDomainDelete -v`
Expected: FAIL — routes don't exist

- [ ] **Step 3: Add archive/restore/delete dashboard routes**

In `src/dashboard/routes.py`:

```python
@router.post("/domains/{name}/archive")
async def domain_archive(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Archive a domain from the dashboard."""


    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        domain.archived_at = datetime.now(UTC)
        audit(session, EventType.DOMAIN_ARCHIVED, domain_name=name, source="dashboard")
        await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/restore")
async def domain_restore(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Restore an archived domain from the dashboard."""


    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    domain.archived_at = None
    audit(session, EventType.DOMAIN_RESTORED, domain_name=name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/delete")
async def domain_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Hard-delete an archived domain with no watches."""


    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        raise HTTPException(status_code=409, detail="Archive the domain before deleting it")

    watch_result = await session.execute(
        select(Watch).where(Watch.effective_domain == name).limit(1)
    )
    if watch_result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watches still reference domain '{name}'",
        )

    audit(session, EventType.DOMAIN_DELETED, domain_name=name, source="dashboard")
    await session.delete(domain)
    await session.commit()

    return RedirectResponse(url="/domains", status_code=303)
```

Add `datetime` and `UTC` imports at top of file:

```python
from datetime import UTC, datetime
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_domain_routes.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_domain_routes.py
git commit -m "#41 feat: domain archive, restore, delete dashboard routes"
```

---

## Task 12: Full integration test pass and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All PASS. Fix any failures.

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: No errors. Fix any issues.

- [ ] **Step 3: Run format check**

Run: `uv run ruff format --check .`
Expected: No reformatting needed.

- [ ] **Step 4: Build Tailwind CSS**

Run: `bash scripts/build_css.sh` (or the project's CSS build command)
Expected: Compiled CSS includes any new utility classes.

- [ ] **Step 5: Manual smoke test**

Start the dev server:
```bash
export $(cat env | xargs) && uv run alembic upgrade head && uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify at `https://watcher.exe.xyz/domains`:
- Search and filter work
- Pagination works
- "New Domain" creates via probe
- Detail view shows inline-editable fields
- Archive/restore/delete lifecycle works
- Flash messages appear correctly

- [ ] **Step 6: Commit any cleanup**

```bash
git add -A
git commit -m "#41 chore: lint and CSS build for domains CRUD"
```
