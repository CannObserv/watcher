# WatchedItem CRUD UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operators can list, view, and edit WatchedItems (defaults, name, description, notification templates, archive lifecycle, sub_aspect review) from `/api/v1/watched-items` and `/watched-items` without touching the DB.

**Architecture:** Five sequential slices — API → list page → detail page (read-only) → defaults editor (inline-edit) → notification templates + sub_aspect review. The `watched_items` schema is already in place (#160); no migrations. Reuse the `_watch_field_*` inline-edit pattern, the `notification_template_row.html` table-row pattern, and the per-Watch notification-config route shapes verbatim where possible. The InfoItem summary card calls `ArchiverClient.get_info_item` per detail-page load; tests mock the SDK via the registry-override fixture.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), Pydantic v2, Jinja2 + HTMX, Tailwind CSS, pytest, `archiver-client` SDK.

**Spec:** [GH #161](https://github.com/CannObserv/watcher/issues/161). Follow-up #164 for bulk "Add Watches for new sub_aspects" is out of scope here.

**Design references:**
- [docs/plans/2026-05-15-watched-item-infoitem-first-design.md](2026-05-15-watched-item-infoitem-first-design.md) Section 5.3 (operator surface).
- [AGENTS.md](../../AGENTS.md) — TDD requirement, conventions.

---

## Scope Check

Five slices, but they share a single feature surface (WatchedItem CRUD), a single router (`/watched-items`), and a single model (`WatchedItem` + `WatchedItemNotificationTemplate`). Sequential dependencies (later slices import context helpers and templates from earlier slices) make this one plan, not five.

If implemented out of order: slice 4 depends on slice 3's template scaffolding; slice 5 depends on slice 3's danger-zone pattern and slice 1's API for the template CRUD. Slice 1 must land before any dashboard work because the dashboard tests reuse the API for setup data.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `src/api/schemas/watched_item.py` | Pydantic schemas: `WatchedItemPatch`, `WatchedItemResponse`, `WatchedItemTemplatePatch`, `WatchedItemTemplateCreate`, `WatchedItemTemplateResponse`. |
| `src/api/routes/watched_items.py` | API routes: list, get, patch, archive, restore, mark-reviewed, template CRUD. |
| `src/dashboard/templates/pages/watched_items.html` | List page. |
| `src/dashboard/templates/pages/watched_item_detail.html` | Detail page (header, summary card, defaults editor section, templates section, child watches, sub_aspect banner, danger zone). |
| `src/dashboard/templates/partials/watched_item_field.html` | Inline-edit field partial (mirrors `partials/watch_field.html`). |
| `src/dashboard/templates/partials/watched_item_tags_editor.html` | Chip-style tag add/remove widget. |
| `src/dashboard/templates/partials/watched_item_templates.html` | Templates table partial (rows). |
| `src/dashboard/templates/partials/watched_item_template_row.html` | Single template row (mirrors `notification_template_row.html`). |
| `src/dashboard/templates/partials/watched_item_template_form.html` | Add/edit template form partial. |
| `src/dashboard/templates/partials/watched_item_subaspect_banner.html` | "N new sub_aspects" banner. |
| `src/dashboard/templates/partials/watched_item_info_item_card.html` | Read-only InfoItem summary (name, primary URL, binding tree). |
| `tests/api/test_watched_items.py` | Integration tests for the API surface. |
| `tests/dashboard/test_watched_item_routes.py` | Integration tests for dashboard pages + inline-edit + danger zone. |
| `tests/dashboard/test_watched_item_templates.py` | Integration tests for template CRUD partials. |

### Modified files

| File | Change |
|---|---|
| `src/core/models/audit_log.py` | Add `WATCHED_ITEM_UPDATED`, `WATCHED_ITEM_ARCHIVED`, `WATCHED_ITEM_RESTORED`, `WATCHED_ITEM_REVIEWED`, `WATCHED_ITEM_TEMPLATE_*` event types. |
| `src/api/main.py` | `v1_router.include_router(watched_items_router)`. |
| `src/dashboard/context.py` | Add `get_watched_item_list`, `get_watched_item_detail`, `get_watched_item_templates`, `count_new_subaspects`. |
| `src/dashboard/routes.py` | Add page routes (list, detail), inline-edit routes (`/field/{field_name}`), archive/restore/mark-reviewed routes, template CRUD routes. |
| `src/dashboard/templates/base.html` | Add `<a href="/watched-items">` to sidebar nav. |

### Not modified (intentionally)

- `src/core/models/watched_item.py` — every field this plan touches is already present.
- `src/core/models/watched_item_notification_template.py` — schema final from #160.
- `alembic/` — no migrations needed.

---

## Conventions

- **TDD:** every task starts with a failing test. No production code without a red test first.
- **Commits:** one per task per the project convention `#161 [type]: <description>` from [AGENTS.md](../../AGENTS.md).
- **Tests:** integration tests use the `client` fixture (real DB, real ASGI). Mark with `pytest.mark.integration`. Dashboard partial tests follow the mocking pattern in [tests/dashboard/test_watch_notifications_partial.py](../../tests/dashboard/test_watch_notifications_partial.py).
- **InfoItem SDK calls:** routes call `get_registry().get_archiver_client().get_info_item(...)`; tests use the existing `info_client` fixture from [tests/conftest.py:425](../../tests/conftest.py#L425) which patches the registry's cached client.
- **Empty form fields:** the inline-edit POST endpoints treat `value=""` as "clear" (set field to `None`) for nullable fields; reject as 400 for required fields like `name`.
- **HTMX:** every mutation route checks `HX-Request` and serves a partial; non-HTMX requests redirect to the detail page (`303`).
- **Audit:** every mutation calls `audit(session, EventType.X, ...)` before commit. Don't add a `watched_item_id` FK to AuditLog — pass `watched_item_id=<ulid>` in the payload kwargs to match existing template/domain audit patterns.

---

## SLICE 1 — API (`/api/v1/watched-items`)

Self-contained; ships operator-usable surface before any UI exists.

### Task 1: Audit event types

**Files:**
- Modify: `src/core/models/audit_log.py:48` (after `WATCHED_ITEM_THROTTLED`)
- Test: `tests/api/test_audit_log.py`

- [ ] **Step 1: Write failing test**

Add to `tests/api/test_audit_log.py`:

```python
class TestWatchedItemEventTypes:
    def test_watched_item_event_constants_exist(self):
        from src.core.models.audit_log import EventType
        assert EventType.WATCHED_ITEM_UPDATED == "watched_item.updated"
        assert EventType.WATCHED_ITEM_ARCHIVED == "watched_item.archived"
        assert EventType.WATCHED_ITEM_RESTORED == "watched_item.restored"
        assert EventType.WATCHED_ITEM_REVIEWED == "watched_item.reviewed"
        assert EventType.WATCHED_ITEM_TEMPLATE_CREATED == "watched_item_template.created"
        assert EventType.WATCHED_ITEM_TEMPLATE_UPDATED == "watched_item_template.updated"
        assert EventType.WATCHED_ITEM_TEMPLATE_DELETED == "watched_item_template.deleted"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/api/test_audit_log.py::TestWatchedItemEventTypes -v --no-cov`
Expected: FAIL (`AttributeError`).

- [ ] **Step 3: Add constants**

In `src/core/models/audit_log.py` inside class `EventType`, after `WATCHED_ITEM_THROTTLED`:

```python
    WATCHED_ITEM_UPDATED = "watched_item.updated"
    WATCHED_ITEM_ARCHIVED = "watched_item.archived"
    WATCHED_ITEM_RESTORED = "watched_item.restored"
    WATCHED_ITEM_REVIEWED = "watched_item.reviewed"
    WATCHED_ITEM_TEMPLATE_CREATED = "watched_item_template.created"
    WATCHED_ITEM_TEMPLATE_UPDATED = "watched_item_template.updated"
    WATCHED_ITEM_TEMPLATE_DELETED = "watched_item_template.deleted"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api/test_audit_log.py::TestWatchedItemEventTypes -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/models/audit_log.py tests/api/test_audit_log.py
git commit -m "#161 feat: add WatchedItem audit event types"
```

---

### Task 2: Pydantic schemas

**Files:**
- Create: `src/api/schemas/watched_item.py`
- Test: `tests/api/schemas/test_watched_item_schemas.py`

- [ ] **Step 1: Write failing test**

Create `tests/api/schemas/test_watched_item_schemas.py`:

```python
"""Pydantic schema tests for WatchedItem API."""
import pytest
from pydantic import ValidationError


class TestWatchedItemPatch:
    def test_accepts_all_optional_fields(self):
        from src.api.schemas.watched_item import WatchedItemPatch
        p = WatchedItemPatch(
            name="Renamed",
            description="notes",
            default_schedule_config={"interval": "30m"},
            default_content_type="html",
            default_tags=["a", "b"],
        )
        assert p.name == "Renamed"
        assert p.default_schedule_config == {"interval": "30m"}

    def test_all_fields_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch
        assert WatchedItemPatch().model_dump(exclude_unset=True) == {}

    def test_name_rejects_empty(self):
        from src.api.schemas.watched_item import WatchedItemPatch
        with pytest.raises(ValidationError):
            WatchedItemPatch(name="")

    def test_invalid_content_type(self):
        from src.api.schemas.watched_item import WatchedItemPatch
        with pytest.raises(ValidationError):
            WatchedItemPatch(default_content_type="bogus")


class TestWatchedItemResponse:
    def test_constructs_from_attributes(self):
        from src.api.schemas.watched_item import WatchedItemResponse
        from src.core.models.watched_item import WatchedItem
        from ulid import ULID
        wi = WatchedItem(info_item_id=ULID(), name="X")
        wi.id = ULID()
        from datetime import UTC, datetime
        wi.created_at = wi.updated_at = datetime.now(UTC)
        r = WatchedItemResponse.model_validate(wi)
        assert r.name == "X"


class TestTemplateSchemas:
    def test_template_create_defaults(self):
        from src.api.schemas.watched_item import WatchedItemTemplateCreate
        c = WatchedItemTemplateCreate(channel_hint="mailto://x@y.z")
        assert c.events == ["change_detected"]
        assert c.is_active is True

    def test_template_create_rejects_empty_channel(self):
        from src.api.schemas.watched_item import WatchedItemTemplateCreate
        with pytest.raises(ValidationError):
            WatchedItemTemplateCreate(channel_hint="")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/schemas/test_watched_item_schemas.py -v --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Create the schema module**

`src/api/schemas/watched_item.py`:

```python
"""Pydantic schemas for WatchedItem and WatchedItemNotificationTemplate API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.api.schemas.validators import validate_event_list
from src.core.models.watch import ContentType


class WatchedItemPatch(BaseModel):
    """Partial update to a WatchedItem. All fields optional."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    default_schedule_config: dict | None = None
    default_content_type: str | None = None
    default_tags: list[str] | None = None

    @field_validator("default_content_type")
    @classmethod
    def _ct(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            ContentType(v)
        except ValueError as exc:
            raise ValueError(f"Invalid default_content_type: {v!r}") from exc
        return v


class WatchedItemResponse(BaseModel):
    """Single WatchedItem record."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    info_item_id: ULIDStr
    name: str
    description: str | None
    is_active: bool
    archived_at: datetime | None
    last_reviewed_at: datetime | None
    default_schedule_config: dict | None
    default_content_type: str | None
    default_tags: list[str] | None
    created_at: datetime
    updated_at: datetime


class WatchedItemTemplateCreate(BaseModel):
    """Create a notification template under a WatchedItem."""

    title: str | None = Field(None, max_length=100)
    channel_hint: str = Field(..., min_length=1, max_length=50)
    events: list[str] = Field(default_factory=lambda: ["change_detected"])
    is_active: bool = True
    content_config: dict | None = None
    remote_channel_id: str | None = Field(None, max_length=26)

    @field_validator("events")
    @classmethod
    def _ev(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class WatchedItemTemplatePatch(BaseModel):
    """Partial update to a WatchedItemNotificationTemplate."""

    title: str | None = Field(None, max_length=100)
    channel_hint: str | None = Field(None, min_length=1, max_length=50)
    events: list[str] | None = None
    is_active: bool | None = None
    content_config: dict | None = None
    remote_channel_id: str | None = Field(None, max_length=26)

    @field_validator("events")
    @classmethod
    def _ev(cls, v: list[str] | None) -> list[str] | None:
        return validate_event_list(v) if v is not None else None


class WatchedItemTemplateResponse(BaseModel):
    """Single notification template under a WatchedItem."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watched_item_id: ULIDStr
    title: str | None
    channel_hint: str
    events: list[str]
    is_active: bool
    content_config: dict | None
    remote_channel_id: str | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api/schemas/test_watched_item_schemas.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas/watched_item.py tests/api/schemas/test_watched_item_schemas.py
git commit -m "#161 feat: WatchedItem API Pydantic schemas"
```

---

### Task 3: Route module — list, get, patch

**Files:**
- Create: `src/api/routes/watched_items.py`
- Modify: `src/api/main.py:85` (after `v1_router.include_router(domains_router)`)
- Test: `tests/api/test_watched_items.py`

- [ ] **Step 1: Write failing tests**

Create `tests/api/test_watched_items.py`:

```python
"""Integration tests for WatchedItem API endpoints."""

import pytest

pytestmark = pytest.mark.integration


async def _make_watched_item(db_session, **overrides):
    """Helper: create a WatchedItem + parent InfoItem via the test fixtures."""
    from tests.conftest import make_info_item
    from src.core.models.watched_item import WatchedItem
    item = await make_info_item(db_session)
    wi = WatchedItem(info_item_id=item.info_item_id, name=overrides.pop("name", "Test WI"))
    for k, v in overrides.items():
        setattr(wi, k, v)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestListWatchedItems:
    async def test_empty_list(self, client):
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_returns_items(self, client, db_session):
        await _make_watched_item(db_session, name="Alpha")
        await _make_watched_item(db_session, name="Beta")
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert {"Alpha", "Beta"} <= set(names)

    async def test_archived_excluded_by_default(self, client, db_session):
        from datetime import UTC, datetime
        await _make_watched_item(db_session, name="Active")
        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items")
        names = [r["name"] for r in response.json()]
        assert "Active" in names
        assert "Archived" not in names

    async def test_archived_included_when_requested(self, client, db_session):
        from datetime import UTC, datetime
        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items?include_archived=true")
        names = [r["name"] for r in response.json()]
        assert "Archived" in names


class TestGetWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID
        response = await client.get(f"/api/v1/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Single")
        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Single"


class TestPatchWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID
        response = await client.patch(f"/api/v1/watched-items/{ULID()}", json={"name": "x"})
        assert response.status_code == 404

    async def test_rename(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Old")
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"name": "New"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "New"

    async def test_update_schedule(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "30m"}},
        )
        assert response.status_code == 200
        assert response.json()["default_schedule_config"] == {"interval": "30m"}

    async def test_update_tags(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"default_tags": ["a", "b"]}
        )
        assert response.json()["default_tags"] == ["a", "b"]

    async def test_empty_patch_is_noop(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Stays")
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={})
        assert response.status_code == 200
        assert response.json()["name"] == "Stays"

    async def test_invalid_content_type(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_content_type": "bogus"},
        )
        assert response.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_watched_items.py -v --no-cov -m integration`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Create the route module**

`src/api/routes/watched_items.py`:

```python
"""WatchedItem CRUD API endpoints (#161)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.deps import get_db_session
from src.api.routes.helpers import parse_ulid
from src.api.schemas.watched_item import WatchedItemPatch, WatchedItemResponse
from src.core.models.audit_log import EventType, audit
from src.core.models.watched_item import WatchedItem

router = APIRouter(prefix="/watched-items", tags=["watched-items"])


async def _get_or_404(session: AsyncSession, wi_id: str) -> WatchedItem:
    wi_ulid = parse_ulid(wi_id)
    wi = await session.get(WatchedItem, wi_ulid)
    if wi is None:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    return wi


@router.get("", response_model=list[WatchedItemResponse])
async def list_watched_items(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_db_session),
):
    """List WatchedItems. Archived excluded unless ``include_archived=true``."""
    stmt = select(WatchedItem).order_by(WatchedItem.name)
    if not include_archived:
        stmt = stmt.where(WatchedItem.archived_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{watched_item_id}", response_model=WatchedItemResponse)
async def get_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Fetch a single WatchedItem by ID."""
    return await _get_or_404(session, watched_item_id)


@router.patch("/{watched_item_id}", response_model=WatchedItemResponse)
async def patch_watched_item(
    watched_item_id: str,
    data: WatchedItemPatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Update mutable WatchedItem fields. All fields optional."""
    wi = await _get_or_404(session, watched_item_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(wi, field, value)
    if updates:
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=sorted(updates.keys()),
            source="api",
        )
    await session.commit()
    await session.refresh(wi)
    return wi
```

- [ ] **Step 4: Register router**

In `src/api/main.py`, after `v1_router.include_router(domains_router)`:

```python
from src.api.routes.watched_items import router as watched_items_router

v1_router.include_router(watched_items_router)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/api/test_watched_items.py -v --no-cov -m integration`
Expected: PASS for all in `TestListWatchedItems`, `TestGetWatchedItem`, `TestPatchWatchedItem`.

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/watched_items.py src/api/main.py tests/api/test_watched_items.py
git commit -m "#161 feat: WatchedItem API list/get/patch"
```

---

### Task 4: Archive / restore / mark-reviewed (with cascade)

**Files:**
- Modify: `src/api/routes/watched_items.py`
- Test: `tests/api/test_watched_items.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/api/test_watched_items.py`:

```python
class TestArchiveRestore:
    async def test_archive_marks_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        data = response.json()
        assert data["archived_at"] is not None
        assert data["is_active"] is False

    async def test_archive_cascades_to_child_watches(self, client, db_session):
        from tests.conftest import make_watch
        wi = await _make_watched_item(db_session)
        w1 = await make_watch(db_session, name="C1", watched_item=wi)
        w2 = await make_watch(db_session, name="C2", watched_item=wi)
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        # Reload children and confirm cascade
        await db_session.refresh(w1)
        await db_session.refresh(w2)
        assert w1.is_active is False and w1.is_archived is True
        assert w2.is_active is False and w2.is_archived is True

    async def test_restore_parent_only(self, client, db_session):
        from datetime import UTC, datetime
        from tests.conftest import make_watch
        wi = await _make_watched_item(
            db_session, archived_at=datetime.now(UTC), is_active=False
        )
        w = await make_watch(
            db_session, name="ChildArchived", watched_item=wi,
            is_active=False, is_archived=True,
        )
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")
        assert response.status_code == 200
        assert response.json()["archived_at"] is None
        await db_session.refresh(w)
        # Restore is parent-only — children stay archived.
        assert w.is_archived is True

    async def test_archive_404(self, client):
        from ulid import ULID
        response = await client.post(f"/api/v1/watched-items/{ULID()}/archive")
        assert response.status_code == 404


class TestMarkReviewed:
    async def test_stamps_now(self, client, db_session):
        wi = await _make_watched_item(db_session)
        before = wi.last_reviewed_at
        response = await client.post(f"/api/v1/watched-items/{wi.id}/mark-reviewed")
        assert response.status_code == 200
        stamped = response.json()["last_reviewed_at"]
        assert stamped is not None
        assert before is None or stamped > before.isoformat()

    async def test_404(self, client):
        from ulid import ULID
        response = await client.post(
            f"/api/v1/watched-items/{ULID()}/mark-reviewed"
        )
        assert response.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_watched_items.py::TestArchiveRestore tests/api/test_watched_items.py::TestMarkReviewed -v --no-cov -m integration`
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Add the three endpoints**

Append to `src/api/routes/watched_items.py`:

```python
from src.core.models.watch import Watch


@router.post("/{watched_item_id}/archive", response_model=WatchedItemResponse)
async def archive_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Archive a WatchedItem and cascade-archive all child Watches.

    The cascade flips ``is_active`` to False and ``is_archived`` to True on
    every child Watch in a single transaction; the WatchedItem's fetch
    cycle stops within one ``schedule_tick`` interval because the tick
    filters on ``WatchedItem.archived_at IS NULL``.
    """
    wi = await _get_or_404(session, watched_item_id)
    now = datetime.now(UTC)

    if wi.archived_at is None:
        wi.archived_at = now
        wi.is_active = False
        audit(
            session,
            EventType.WATCHED_ITEM_ARCHIVED,
            watched_item_id=str(wi.id),
            source="api",
        )
        result = await session.execute(
            select(Watch).where(Watch.watched_item_id == wi.id)
        )
        for child in result.scalars().all():
            if not child.is_archived:
                child.is_active = False
                child.is_archived = True
                audit(
                    session,
                    EventType.WATCH_ARCHIVED,
                    watch_id=child.id,
                    cascade_from_watched_item_id=str(wi.id),
                    source="api",
                )

    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/restore", response_model=WatchedItemResponse)
async def restore_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Restore the WatchedItem only. Child Watches stay archived."""
    wi = await _get_or_404(session, watched_item_id)
    if wi.archived_at is not None:
        wi.archived_at = None
        wi.is_active = True
        audit(
            session,
            EventType.WATCHED_ITEM_RESTORED,
            watched_item_id=str(wi.id),
            source="api",
        )
    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/mark-reviewed", response_model=WatchedItemResponse)
async def mark_reviewed(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Stamp ``last_reviewed_at = now()``."""
    wi = await _get_or_404(session, watched_item_id)
    wi.last_reviewed_at = datetime.now(UTC)
    audit(
        session,
        EventType.WATCHED_ITEM_REVIEWED,
        watched_item_id=str(wi.id),
        source="api",
    )
    await session.commit()
    await session.refresh(wi)
    return wi
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/api/test_watched_items.py -v --no-cov -m integration`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/watched_items.py tests/api/test_watched_items.py
git commit -m "#161 feat: WatchedItem archive (cascade) / restore / mark-reviewed"
```

---

### Task 5: Notification template CRUD endpoints

**Files:**
- Modify: `src/api/routes/watched_items.py`
- Test: `tests/api/test_watched_items.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/api/test_watched_items.py`:

```python
class TestTemplateCrud:
    async def test_list_empty(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.get(f"/api/v1/watched-items/{wi.id}/notification-templates")
        assert response.status_code == 200
        assert response.json() == []

    async def test_create_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={
                "title": "Email Greg",
                "channel_hint": "mailto://x:y@z",
                "events": ["change_detected"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Email Greg"
        assert data["watched_item_id"] == str(wi.id)

    async def test_create_404_unknown_parent(self, client):
        from ulid import ULID
        response = await client.post(
            f"/api/v1/watched-items/{ULID()}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        assert response.status_code == 404

    async def test_patch_updates(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}",
            json={"is_active": False, "title": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert response.json()["title"] == "Renamed"

    async def test_delete(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.delete(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}"
        )
        assert response.status_code == 204
        # Verify gone
        listing = await client.get(
            f"/api/v1/watched-items/{wi.id}/notification-templates"
        )
        assert listing.json() == []
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/api/test_watched_items.py::TestTemplateCrud -v --no-cov -m integration`
Expected: FAIL — 404s.

- [ ] **Step 3: Add the endpoints**

Append to `src/api/routes/watched_items.py`:

```python
from src.api.schemas.watched_item import (
    WatchedItemTemplateCreate,
    WatchedItemTemplatePatch,
    WatchedItemTemplateResponse,
)
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)


async def _template_or_404(
    session: AsyncSession, wi: WatchedItem, tpl_id: str
) -> WatchedItemNotificationTemplate:
    tpl = await session.get(
        WatchedItemNotificationTemplate, parse_ulid(tpl_id)
    )
    if tpl is None or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.get(
    "/{watched_item_id}/notification-templates",
    response_model=list[WatchedItemTemplateResponse],
)
async def list_templates(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """List notification templates under a WatchedItem."""
    wi = await _get_or_404(session, watched_item_id)
    result = await session.execute(
        select(WatchedItemNotificationTemplate)
        .where(WatchedItemNotificationTemplate.watched_item_id == wi.id)
        .order_by(WatchedItemNotificationTemplate.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "/{watched_item_id}/notification-templates",
    response_model=WatchedItemTemplateResponse,
    status_code=201,
)
async def create_template(
    watched_item_id: str,
    data: WatchedItemTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification template under a WatchedItem."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = WatchedItemNotificationTemplate(
        watched_item_id=wi.id,
        **data.model_dump(),
    )
    session.add(tpl)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_CREATED,
        watched_item_id=str(wi.id),
        source="api",
    )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.patch(
    "/{watched_item_id}/notification-templates/{tpl_id}",
    response_model=WatchedItemTemplateResponse,
)
async def patch_template(
    watched_item_id: str,
    tpl_id: str,
    data: WatchedItemTemplatePatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Update fields on an existing template."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = await _template_or_404(session, wi, tpl_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(tpl, field, value)
    if updates:
        audit(
            session,
            EventType.WATCHED_ITEM_TEMPLATE_UPDATED,
            watched_item_id=str(wi.id),
            template_id=str(tpl.id),
            updated_fields=sorted(updates.keys()),
            source="api",
        )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete(
    "/{watched_item_id}/notification-templates/{tpl_id}", status_code=204
)
async def delete_template(
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a template."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = await _template_or_404(session, wi, tpl_id)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_DELETED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="api",
    )
    await session.delete(tpl)
    await session.commit()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/api/test_watched_items.py -v --no-cov -m integration`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/watched_items.py tests/api/test_watched_items.py
git commit -m "#161 feat: WatchedItem notification-template CRUD endpoints"
```

---

## SLICE 2 — Dashboard list page

### Task 6: Context helper — `get_watched_item_list`

**Files:**
- Modify: `src/dashboard/context.py`
- Test: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing test**

Append to `tests/dashboard/test_context.py`:

```python
class TestGetWatchedItemList:
    async def test_excludes_archived_by_default(self, db_session):
        from datetime import UTC, datetime
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_list
        from tests.conftest import make_info_item

        item_a = await make_info_item(db_session)
        item_b = await make_info_item(db_session)
        db_session.add_all([
            WatchedItem(info_item_id=item_a.info_item_id, name="Active"),
            WatchedItem(
                info_item_id=item_b.info_item_id, name="Archived",
                archived_at=datetime.now(UTC), is_active=False,
            ),
        ])
        await db_session.flush()
        results = await get_watched_item_list(db_session)
        names = [wi.name for wi in results]
        assert "Active" in names
        assert "Archived" not in names

    async def test_include_archived(self, db_session):
        from datetime import UTC, datetime
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_list
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        db_session.add(WatchedItem(
            info_item_id=item.info_item_id, name="Arc",
            archived_at=datetime.now(UTC), is_active=False,
        ))
        await db_session.flush()
        results = await get_watched_item_list(db_session, include_archived=True)
        assert any(wi.name == "Arc" for wi in results)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_context.py::TestGetWatchedItemList -v --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement helper**

In `src/dashboard/context.py`, add an import at top:

```python
from src.core.models.watched_item import WatchedItem
```

Append at end of file:

```python
async def get_watched_item_list(
    session: AsyncSession,
    include_archived: bool = False,
) -> list[WatchedItem]:
    """Fetch WatchedItems for dashboard list display."""
    stmt = select(WatchedItem).order_by(WatchedItem.name)
    if not include_archived:
        stmt = stmt.where(WatchedItem.archived_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/dashboard/test_context.py::TestGetWatchedItemList -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#161 feat: get_watched_item_list context helper"
```

---

### Task 7: List page route + template + empty state

**Files:**
- Create: `src/dashboard/templates/pages/watched_items.html`
- Modify: `src/dashboard/routes.py` (add list-page route)
- Modify: `src/dashboard/templates/base.html` (sidebar link)
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/dashboard/test_watched_item_routes.py`:

```python
"""Integration tests for WatchedItem dashboard routes."""

import pytest

pytestmark = pytest.mark.integration


class TestListPage:
    async def test_returns_200(self, client):
        response = await client.get("/watched-items")
        assert response.status_code == 200

    async def test_empty_state_renders_cta(self, client):
        response = await client.get("/watched-items")
        body = response.content
        # Empty state copy + CTA to /watches/new
        assert b"No watched items yet" in body
        assert b"/watches/new" in body

    async def test_list_renders_items(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        db_session.add(WatchedItem(info_item_id=item.info_item_id, name="Listed"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"Listed" in response.content

    async def test_sidebar_link_present(self, client):
        response = await client.get("/")
        assert b'href="/watched-items"' in response.content
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestListPage -v --no-cov -m integration`
Expected: FAIL — 404 on `/watched-items`.

- [ ] **Step 3: Create the list template**

`src/dashboard/templates/pages/watched_items.html`:

```html
{% extends "base.html" %}
{% block title %}Watched Items — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6 flex-wrap gap-4">
  <h2 class="text-2xl font-semibold text-gray-900 dark:text-white">Watched Items</h2>
</div>

{% if watched_items %}
<div class="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
  <table class="data-table">
    <thead>
      <tr>
        <th scope="col">Name</th>
        <th scope="col">InfoItem</th>
        <th scope="col">Interval</th>
        <th scope="col">Content Type</th>
        <th scope="col">Tags</th>
        <th scope="col">Status</th>
        <th scope="col">Last Reviewed</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
    {% for wi in watched_items %}
      <tr>
        <td class="font-medium">
          <a href="/watched-items/{{ wi.id }}" class="link">{{ wi.name }}</a>
        </td>
        <td class="text-xs text-gray-500 dark:text-gray-400 font-mono">{{ wi.info_item_id }}</td>
        <td>{{ (wi.default_schedule_config or {}).get("interval") or "—" }}</td>
        <td>{{ (wi.default_content_type or "—")|upper }}</td>
        <td>
          {% if wi.default_tags %}
            <div class="chip-group">
            {% for t in wi.default_tags %}<span class="chip">{{ t }}</span>{% endfor %}
            </div>
          {% else %}<span class="text-gray-400">—</span>{% endif %}
        </td>
        <td>
          {% if wi.archived_at %}<span class="badge badge-archived">Archived</span>
          {% elif wi.is_active %}<span class="badge badge-active">Active</span>
          {% else %}<span class="badge badge-inactive">Inactive</span>{% endif %}
        </td>
        <td class="text-gray-500 dark:text-gray-400">
          {% if wi.last_reviewed_at %}{{ wi.last_reviewed_at.strftime('%Y-%m-%d') }}
          {% else %}Never{% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<div class="stat-card text-center py-12">
  <p class="text-lg font-medium text-gray-900 dark:text-white mb-2">No watched items yet</p>
  <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
    A WatchedItem is auto-created when you add the first Watch under an Archiver InfoItem.
  </p>
  <a href="/watches/new" class="btn btn-primary">Create your first Watch</a>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Add the route**

In `src/dashboard/routes.py`, import:

```python
from src.dashboard.context import (
    ...,
    get_watched_item_list,
)
```

Add the route (place after the watches list block, before domain routes):

```python
@router.get("/watched-items")
async def watched_items_page(
    request: Request,
    include_archived: bool = False,
    session: AsyncSession = Depends(get_db_session),
):
    """List page for WatchedItems."""
    watched_items = await get_watched_item_list(session, include_archived=include_archived)
    return templates.TemplateResponse(
        request,
        "pages/watched_items.html",
        {
            "request": request,
            "active_page": "watched-items",
            "watched_items": watched_items,
            "include_archived": include_archived,
            "flash": None,
        },
    )
```

- [ ] **Step 5: Add sidebar link**

In `src/dashboard/templates/base.html`, find the sidebar `<nav>` and add (after the Watches link):

```html
<a href="/watched-items"
   class="{% if active_page == 'watched-items' %}sidebar-link sidebar-link-active{% else %}sidebar-link{% endif %}">
  Watched Items
</a>
```

(Use whatever class names match the existing pattern; copy from the Watches link.)

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestListPage -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 7: Rebuild CSS**

```bash
bash scripts/build-css.sh
```

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/watched_items.html src/dashboard/templates/base.html src/dashboard/static/css/output.css tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem list page with empty state"
```

---

## SLICE 3 — Detail page (read-only) + InfoItem summary + danger zone

### Task 8: Context helpers — detail + templates loader

**Files:**
- Modify: `src/dashboard/context.py`
- Test: `tests/dashboard/test_context.py`

- [ ] **Step 1: Write failing test**

Append to `tests/dashboard/test_context.py`:

```python
class TestGetWatchedItemDetail:
    async def test_returns_record(self, db_session):
        from src.core.models.watched_item import WatchedItem
        from src.dashboard.context import get_watched_item_detail
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="X")
        db_session.add(wi)
        await db_session.flush()
        loaded = await get_watched_item_detail(db_session, str(wi.id))
        assert loaded is not None
        assert loaded.name == "X"

    async def test_unknown_returns_none(self, db_session):
        from ulid import ULID
        from src.dashboard.context import get_watched_item_detail
        assert await get_watched_item_detail(db_session, str(ULID())) is None
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_context.py::TestGetWatchedItemDetail -v --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement helpers**

Append to `src/dashboard/context.py`:

```python
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)


async def get_watched_item_detail(
    session: AsyncSession, watched_item_id: str
) -> WatchedItem | None:
    """Fetch a single WatchedItem; returns None on invalid ID or not-found."""
    try:
        wi_ulid = ULID.from_str(watched_item_id)
    except (ValueError, TypeError):
        return None
    return await session.get(WatchedItem, wi_ulid)


async def get_watched_item_templates(
    session: AsyncSession, watched_item_id: ULID
) -> list[WatchedItemNotificationTemplate]:
    """Load notification templates under a WatchedItem (created_at asc)."""
    result = await session.execute(
        select(WatchedItemNotificationTemplate)
        .where(WatchedItemNotificationTemplate.watched_item_id == watched_item_id)
        .order_by(WatchedItemNotificationTemplate.created_at)
    )
    return list(result.scalars().all())
```

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/dashboard/test_context.py::TestGetWatchedItemDetail -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/context.py tests/dashboard/test_context.py
git commit -m "#161 feat: get_watched_item_detail + templates loader"
```

---

### Task 9: Detail page route — read-only sections + InfoItem summary

**Files:**
- Create: `src/dashboard/templates/pages/watched_item_detail.html`
- Create: `src/dashboard/templates/partials/watched_item_info_item_card.html`
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_watched_item_routes.py`:

```python
class TestDetailPage:
    async def test_returns_200_with_archiver_mock(self, client, db_session, info_client):
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Detail Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        info_client.get_info_item = AsyncMock(return_value=_fake_info_item_out(
            info_item_id=str(item.info_item_id),
        ))

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Detail Test" in response.content

    async def test_404_unknown(self, client):
        from ulid import ULID
        response = await client.get(f"/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_renders_info_item_summary(self, client, db_session, info_client):
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Summary Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(return_value=_fake_info_item_out(
            info_item_id=str(item.info_item_id),
            primary_url="https://example.org/foo",
        ))
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"https://example.org/foo" in response.content

    async def test_renders_danger_zone_archive(self, client, db_session, info_client):
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Danger")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(return_value=_fake_info_item_out(
            info_item_id=str(item.info_item_id),
        ))
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content


def _fake_info_item_out(*, info_item_id, primary_url="https://example.com"):
    """Minimal InfoItemOut-shaped mock for the summary card."""
    from types import SimpleNamespace
    from datetime import UTC, datetime
    return SimpleNamespace(
        info_item_id=info_item_id,
        name="Fake InfoItem",
        description=None,
        owner=None,
        info_item_sources=[
            SimpleNamespace(
                info_source_id="fake-primary-src",
                role=None,  # primary
                created_at=datetime.now(UTC),
                url=primary_url,
            ),
        ],
    )
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestDetailPage -v --no-cov -m integration`
Expected: FAIL — 404.

- [ ] **Step 3: Create the InfoItem card partial**

`src/dashboard/templates/partials/watched_item_info_item_card.html`:

```html
{# InfoItem summary card. Expects: info_item (SDK InfoItemOut), watched_item. #}
<div class="stat-card mb-6">
  <div class="flex justify-between items-start mb-3">
    <div>
      <p class="text-sm text-gray-500 dark:text-gray-400">Archiver InfoItem</p>
      <p class="font-semibold text-gray-900 dark:text-white">{{ info_item.name }}</p>
    </div>
    <span class="text-xs font-mono text-gray-400">{{ info_item.info_item_id }}</span>
  </div>
  {% if info_item.info_item_sources %}
  <ul class="text-sm space-y-1">
    {% for src in info_item.info_item_sources %}
    <li class="flex items-center gap-2">
      {% if src.role is none %}
      <span class="badge badge-info">primary</span>
      {% elif src.role == "cross_check" %}
      <span class="badge badge-inactive">cross_check</span>
      {% else %}
      <span class="badge badge-active">{{ src.role }}</span>
      {% endif %}
      <span class="font-mono text-xs text-gray-500">{{ src.info_source_id }}</span>
      {% if src.role is none and src.url is defined %}
      <span class="text-gray-600 dark:text-gray-300 truncate">{{ src.url }}</span>
      {% endif %}
    </li>
    {% endfor %}
  </ul>
  {% endif %}
</div>
```

- [ ] **Step 4: Create the detail page template**

`src/dashboard/templates/pages/watched_item_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ watched_item.name }} — watcher{% endblock %}
{% block content %}
<div class="flex justify-between items-center mb-6 flex-wrap gap-4">
  <div>
    <h2 class="text-2xl font-semibold text-gray-900 dark:text-white">{{ watched_item.name }}</h2>
    {% if watched_item.description %}
    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ watched_item.description }}</p>
    {% endif %}
  </div>
  <div>
    {% if watched_item.archived_at %}<span class="badge badge-archived">Archived</span>
    {% elif watched_item.is_active %}<span class="badge badge-active">Active</span>
    {% else %}<span class="badge badge-inactive">Inactive</span>{% endif %}
  </div>
</div>

{% include "partials/watched_item_info_item_card.html" %}

{# Defaults (read-only in this slice; inline-edit added in Slice 4) #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Defaults</h3>
  <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 divide-y divide-gray-200 dark:divide-gray-700 p-4 space-y-2">
    <p><span class="form-label">Interval</span>: {{ (watched_item.default_schedule_config or {}).get("interval") or "—" }}</p>
    <p><span class="form-label">Content Type</span>: {{ (watched_item.default_content_type or "—")|upper }}</p>
    <p>
      <span class="form-label">Tags</span>:
      {% if watched_item.default_tags %}
        <span class="chip-group">{% for t in watched_item.default_tags %}<span class="chip">{{ t }}</span>{% endfor %}</span>
      {% else %}—{% endif %}
    </p>
  </div>
</section>

{# Child Watches table — reuse partial scoped by watched_item_id #}
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Watches</h3>
  {% if child_watches %}
  {% include "partials/watch_table.html" with context %}
  {% else %}
  <p class="text-sm text-gray-500">No watches under this WatchedItem.</p>
  {% endif %}
</section>

{# Metadata #}
<p class="text-xs text-gray-400 dark:text-gray-500 mb-8">
  Metadata · ID: {{ watched_item.id }} · Created: {{ watched_item.created_at.strftime("%Y-%m-%d") }} · Updated: {{ watched_item.updated_at.strftime("%Y-%m-%d") }}
  {% if watched_item.last_reviewed_at %} · Reviewed: {{ watched_item.last_reviewed_at.strftime("%Y-%m-%d") }}{% endif %}
</p>

{# Danger zone #}
<section class="border border-red-200 dark:border-red-800 rounded-lg p-6">
  <h3 class="text-lg font-semibold text-red-600 dark:text-red-400 mb-4">Danger Zone</h3>
  {% if not watched_item.archived_at %}
  <div class="flex items-center justify-between">
    <div>
      <p class="text-sm font-medium text-gray-900 dark:text-white">Archive this WatchedItem</p>
      <p class="text-xs text-gray-500 dark:text-gray-400">Stops the fetch cycle and cascades to all child Watches.</p>
    </div>
    <form method="post" action="/watched-items/{{ watched_item.id }}/archive">
      <button type="submit"
        hx-post="/watched-items/{{ watched_item.id }}/archive"
        hx-confirm="Archive {{ watched_item.name }}? All child Watches will also be archived."
        hx-target="body"
        hx-push-url="true"
        class="btn btn-danger-outline min-h-[44px]">Archive</button>
    </form>
  </div>
  {% else %}
  <div class="flex items-center justify-between">
    <div>
      <p class="text-sm font-medium text-gray-900 dark:text-white">Restore this WatchedItem</p>
      <p class="text-xs text-gray-500 dark:text-gray-400">Child Watches stay archived; restore them individually.</p>
    </div>
    <form method="post" action="/watched-items/{{ watched_item.id }}/restore">
      <button type="submit"
        hx-post="/watched-items/{{ watched_item.id }}/restore"
        hx-target="body"
        hx-push-url="true"
        class="btn btn-secondary min-h-[44px]">Restore</button>
    </form>
  </div>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 5: Add the route + child Watches query**

In `src/dashboard/routes.py`, add the route after the list page:

```python
@router.get("/watched-items/{watched_item_id}")
async def watched_item_detail_page(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Detail page for a WatchedItem."""
    from src.dashboard.context import get_watched_item_detail
    from src.core.models.watch import Watch

    wi = await get_watched_item_detail(session, watched_item_id)
    if wi is None:
        return templates.TemplateResponse(
            request, "pages/404.html", {"request": request}, status_code=404
        )

    children = (await session.execute(
        select(Watch).where(Watch.watched_item_id == wi.id).order_by(Watch.name)
    )).scalars().all()

    client_sdk = get_registry().get_archiver_client()
    try:
        info_item = await client_sdk.get_info_item(str(wi.info_item_id))
    except NotFound:
        info_item = None
    except (httpx.ConnectError, ServerError):
        # Archiver down or unreachable — render the page with a placeholder
        # rather than hard-500. The summary card template handles `info_item is None`.
        logger.warning(
            "Archiver unavailable while rendering watched_item detail",
            extra={"watched_item_id": str(wi.id)},
        )
        info_item = None

    return templates.TemplateResponse(
        request,
        "pages/watched_item_detail.html",
        {
            "request": request,
            "active_page": "watched-items",
            "watched_item": wi,
            "info_item": info_item,
            "child_watches": children,
            "watches": children,  # `watch_table.html` reads "watches"
            "flash": None,
        },
    )
```

**Imports for this task** — verify these are already at the top of `src/dashboard/routes.py` (they are in the current file):
- `from archiver_client import AuthError, NotFound, ServerError` ([routes.py:10](../../src/dashboard/routes.py#L10))
- `from fastapi.responses import RedirectResponse, Response` ([routes.py:12](../../src/dashboard/routes.py#L12))
- `from src.core.registry import get_registry` ([routes.py:47](../../src/dashboard/routes.py#L47))

Add at the top if missing (none should be missing today):
- `import httpx`
- `from src.dashboard.context import get_watched_item_detail` (new import for this task)
- `from src.core.models.watch import Watch` (new import for this task)

Move any other inline imports introduced in this task to the top of the file before committing.

Update the InfoItem summary card partial (`watched_item_info_item_card.html`) to gracefully handle `info_item is None`:

```jinja
{% if info_item is none %}
<div class="stat-card mb-6 text-sm text-gray-500">Archiver InfoItem summary unavailable.</div>
{% else %}
  {# existing card markup #}
{% endif %}
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestDetailPage -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 7: Manual smoke**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit https://watcher.exe.xyz:8001/watched-items
# Click into a detail page. Confirm: header, InfoItem card, defaults read-only, child watches, danger zone.
```

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/watched_item_detail.html src/dashboard/templates/partials/watched_item_info_item_card.html src/dashboard/static/css/output.css tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem detail page (read-only) + InfoItem summary"
```

---

### Task 10: Danger zone — archive/restore dashboard routes

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_watched_item_routes.py`:

```python
class TestArchiveRestore:
    async def test_archive_redirects_back(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="ToArchive")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.post(
            f"/watched-items/{wi.id}/archive", follow_redirects=False
        )
        assert response.status_code in (200, 303)

    async def test_archive_cascades_to_child_watches(
        self, client, db_session, info_client
    ):
        from src.core.models.watched_item import WatchedItem
        from src.core.models.watch import Watch
        from tests.conftest import make_info_item, make_watch
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Parent")
        db_session.add(wi)
        await db_session.flush()
        w = await make_watch(db_session, name="Child", watched_item=wi)
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)

        await db_session.refresh(w)
        assert w.is_archived is True

    async def test_restore_clears_archived_at(
        self, client, db_session, info_client
    ):
        from datetime import UTC, datetime
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="Arc",
            archived_at=datetime.now(UTC), is_active=False,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/restore", follow_redirects=False)
        await db_session.refresh(wi)
        assert wi.archived_at is None
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestArchiveRestore -v --no-cov -m integration`
Expected: FAIL — 404.

- [ ] **Step 3: Add dashboard routes that delegate to the API logic**

In `src/dashboard/routes.py`, add:

```python
@router.post("/watched-items/{watched_item_id}/archive")
async def watched_item_archive(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard archive — cascades to child Watches (delegates to shared logic)."""
    from src.api.routes.watched_items import (
        archive_watched_item as _api_archive,
    )
    await _api_archive(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(
        url=f"/watched-items/{watched_item_id}", status_code=303
    )


@router.post("/watched-items/{watched_item_id}/restore")
async def watched_item_restore(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard restore — parent only."""
    from src.api.routes.watched_items import (
        restore_watched_item as _api_restore,
    )
    await _api_restore(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(
        url=f"/watched-items/{watched_item_id}", status_code=303
    )
```

Move the inline imports to the top of the file.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestArchiveRestore -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem dashboard archive (cascade) + restore"
```

---

## SLICE 4 — Defaults editor (inline-edit + chip widget)

### Task 11: Field metadata + helpers

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_watched_item_routes.py`:

```python
class TestFieldHelpers:
    def test_interval_format(self):
        from src.dashboard.routes import _watched_item_field_context
        from unittest.mock import MagicMock
        wi = MagicMock()
        wi.default_schedule_config = {"interval": "15m"}
        ctx = _watched_item_field_context(
            MagicMock(), wi, "default_schedule_interval", mode="view"
        )
        assert ctx["field_value"] == "15m"

    def test_interval_empty_renders_blank(self):
        from src.dashboard.routes import _watched_item_field_context
        from unittest.mock import MagicMock
        wi = MagicMock()
        wi.default_schedule_config = None
        ctx = _watched_item_field_context(
            MagicMock(), wi, "default_schedule_interval", mode="view"
        )
        assert ctx["field_value"] == ""

    def test_apply_interval_writes_into_dict(self):
        from src.dashboard.routes import _apply_watched_item_field_update
        from src.core.models.watched_item import WatchedItem
        from ulid import ULID
        wi = WatchedItem(info_item_id=ULID(), name="x")
        _apply_watched_item_field_update(wi, "default_schedule_interval", "30m")
        assert wi.default_schedule_config == {"interval": "30m"}

    def test_apply_interval_rejects_invalid(self):
        import pytest
        from src.dashboard.routes import _apply_watched_item_field_update
        from src.core.models.watched_item import WatchedItem
        from ulid import ULID
        wi = WatchedItem(info_item_id=ULID(), name="x")
        with pytest.raises(ValueError):
            _apply_watched_item_field_update(wi, "default_schedule_interval", "bogus")

    def test_apply_interval_empty_clears(self):
        from src.dashboard.routes import _apply_watched_item_field_update
        from src.core.models.watched_item import WatchedItem
        from ulid import ULID
        wi = WatchedItem(
            info_item_id=ULID(), name="x",
            default_schedule_config={"interval": "1h"},
        )
        _apply_watched_item_field_update(wi, "default_schedule_interval", "")
        assert wi.default_schedule_config in (None, {})
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestFieldHelpers -v --no-cov`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Add metadata + helpers**

In `src/dashboard/routes.py`, add (near the existing `WATCH_FIELD_META`):

```python
from src.core.scheduler import parse_interval


def _format_interval(wi) -> str:
    cfg = wi.default_schedule_config or {}
    return cfg.get("interval") or ""


def _format_content_type(wi) -> str:
    return wi.default_content_type or ""


WATCHED_ITEM_FIELD_META: dict[str, dict] = {
    "name": {
        "label": "Name",
        "hint": None,
        "type": "text",
        "source": "column",
        "cast": lambda v: v.strip(),
        "format": lambda wi: wi.name,
    },
    "description": {
        "label": "Description",
        "hint": "Optional notes for operators",
        "type": "textarea",
        "source": "column",
        "cast": lambda v: v.strip() or None,
        "format": lambda wi: wi.description or "",
    },
    "default_schedule_interval": {
        "label": "Default Interval",
        "hint": "e.g. 30s, 15m, 6h, 1d. reduce_frequency post-actions may slow this independently.",
        "type": "text",
        "source": "schedule_interval",
        "cast": lambda v: v.strip(),
        "format": _format_interval,
    },
    "default_content_type": {
        "label": "Default Content Type",
        "hint": "Applied to child Watches that don't override.",
        "type": "select",
        "source": "column",
        "cast": lambda v: v.strip() or None,
        "format": _format_content_type,
        "options": [("", "—"), ("html", "HTML"), ("pdf", "PDF")],
    },
}

EDITABLE_WATCHED_ITEM_FIELDS = set(WATCHED_ITEM_FIELD_META.keys())


def _watched_item_field_context(
    request: Request, wi, field_name: str, mode: str = "view"
) -> dict:
    meta = WATCHED_ITEM_FIELD_META[field_name]
    return {
        "watched_item": wi,
        "field_name": field_name,
        "field_label": meta["label"],
        "field_hint": meta.get("hint"),
        "field_value": meta["format"](wi),
        "field_type": meta["type"],
        "field_options": meta.get("options"),
        "field_mode": mode,
    }


def _apply_watched_item_field_update(wi, field_name: str, raw_value: str) -> None:
    meta = WATCHED_ITEM_FIELD_META[field_name]
    cast_fn = meta["cast"]
    typed_value = cast_fn(raw_value)
    source = meta["source"]
    if source == "column":
        setattr(wi, field_name, typed_value)
    elif source == "schedule_interval":
        if not typed_value:
            wi.default_schedule_config = None
        else:
            # Validate interval shape
            parse_interval(typed_value)
            wi.default_schedule_config = {
                **(wi.default_schedule_config or {}),
                "interval": typed_value,
            }
```

- [ ] **Step 4: Verify pass**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestFieldHelpers -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem field metadata + apply helpers"
```

---

### Task 12: Field partial template + macro

**Files:**
- Create: `src/dashboard/templates/partials/watched_item_field.html`
- Modify: `src/dashboard/templates/macros/fields.html`

The existing `macros/fields.html` exposes `watch_field(ctx)` and `domain_field(ctx)` macros that read fields from `ctx` and `{% include %}` the matching partial. We follow the same pattern: a `watched_item_field(ctx)` macro that includes our new partial. Detail templates call the macro, not the partial directly.

- [ ] **Step 1: Create the partial**

Take `src/dashboard/templates/partials/watch_field.html` as the reference. Copy it to `src/dashboard/templates/partials/watched_item_field.html` and make these substitutions:

- Every `watch.id` → `watched_item.id`
- Every `/watches/` → `/watched-items/`
- Remove the `readonly` branch (none of our fields use it; can keep if harmless).
- Remove the `toggle` branch (none of our fields use it).

The partial reads the same variables the existing `watch_field` macro `{% set %}`s into local scope (`field_name`, `field_label`, `field_hint`, `field_value`, `field_type`, `field_options`, `field_mode`), plus a `watched_item` it inherits from caller context. Don't introduce new variable names — keep parity with `watch_field.html`'s shape so the macro is a near-copy.

- [ ] **Step 2: Add the macro**

In `src/dashboard/templates/macros/fields.html`, append after the existing `domain_field` macro (matching the same `{% set ... %}` + `{% include %}` shape):

```jinja
{% macro watched_item_field(ctx) %}
{% set field_name = ctx.field_name %}
{% set field_label = ctx.field_label %}
{% set field_hint = ctx.field_hint %}
{% set field_value = ctx.field_value %}
{% set field_type = ctx.field_type %}
{% set field_options = ctx.field_options %}
{% set field_mode = ctx.field_mode %}
{% include "partials/watched_item_field.html" %}
{% endmacro %}
```

Update the docstring at the top of `macros/fields.html` to mention the new macro alongside `watch_field` and `domain_field`.

There's no direct unit test for the partial/macro — they're exercised by the partial-route test in Task 13.

- [ ] **Step 3: Commit**

```bash
git add src/dashboard/templates/partials/watched_item_field.html src/dashboard/templates/macros/fields.html
git commit -m "#161 feat: watched_item_field partial + macro"
```

---

### Task 13: Inline-edit field routes

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_watched_item_routes.py`:

```python
class TestFieldRoutes:
    async def test_get_field_partial_view_mode(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="FieldTest")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/field/name",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"FieldTest" in response.content

    async def test_post_field_updates(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Old")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/name",
            data={"value": "New"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.name == "New"

    async def test_post_interval_updates_jsonb(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Sched")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/default_schedule_interval",
            data={"value": "45m"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.default_schedule_config == {"interval": "45m"}

    async def test_invalid_interval_rejected(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Sched")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/default_schedule_interval",
            data={"value": "bogus"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    async def test_unknown_field_400(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="X")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/field/nonsense",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestFieldRoutes -v --no-cov -m integration`
Expected: FAIL.

- [ ] **Step 3: Add the routes**

In `src/dashboard/routes.py`:

```python
@router.get("/watched-items/{watched_item_id}/field/{field_name}")
async def watched_item_field_partial(
    request: Request,
    watched_item_id: str,
    field_name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve a single WatchedItem field partial in view or edit mode."""
    if field_name not in EDITABLE_WATCHED_ITEM_FIELDS:
        raise HTTPException(
            status_code=400, detail=f"Field '{field_name}' is not editable"
        )

    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(
            url=f"/watched-items/{watched_item_id}", status_code=303
        )

    ctx = _watched_item_field_context(request, wi, field_name, mode=mode)
    return templates.TemplateResponse(
        request, "partials/watched_item_field.html", ctx
    )


@router.post("/watched-items/{watched_item_id}/field/{field_name}")
async def watched_item_field_update(
    request: Request,
    watched_item_id: str,
    field_name: str,
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single WatchedItem field (HTMX inline edit)."""
    if field_name not in EDITABLE_WATCHED_ITEM_FIELDS:
        raise HTTPException(
            status_code=400, detail=f"Field '{field_name}' is not editable"
        )

    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    if field_name == "name" and not value.strip():
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    try:
        _apply_watched_item_field_update(wi, field_name, value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid value: {exc}"
        ) from exc

    audit(
        session,
        EventType.WATCHED_ITEM_UPDATED,
        watched_item_id=str(wi.id),
        updated_fields=[field_name],
        source="dashboard",
    )
    await session.commit()
    await session.refresh(wi)

    if request.headers.get("HX-Request") == "true":
        ctx = _watched_item_field_context(request, wi, field_name, mode="view")
        return templates.TemplateResponse(
            request, "partials/watched_item_field.html", ctx
        )
    return RedirectResponse(
        url=f"/watched-items/{watched_item_id}", status_code=303
    )
```

- [ ] **Step 4: Wire the fields into the detail template**

Update the detail route to build per-field contexts:

```python
field_contexts = {
    name: _watched_item_field_context(request, wi, name, mode="view")
    for name in ("name", "description", "default_schedule_interval", "default_content_type")
}
```

Pass `field_contexts` in the template context.

Then in `src/dashboard/templates/pages/watched_item_detail.html`, add the macro import at the top (right after `{% extends "base.html" %}`):

```jinja
{% from "macros/fields.html" import watched_item_field with context %}
```

Replace the read-only Defaults block from Task 9 with:

```jinja
<section class="mb-8">
  <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-4">Defaults</h3>
  <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 divide-y divide-gray-200 dark:divide-gray-700">
    {{ watched_item_field(field_contexts["name"]) }}
    {{ watched_item_field(field_contexts["description"]) }}
    {{ watched_item_field(field_contexts["default_schedule_interval"]) }}
    {{ watched_item_field(field_contexts["default_content_type"]) }}
  </div>
</section>
```

This mirrors the call pattern in `watch_detail.html:110` (`{{ watch_field(field_contexts["interval"]) }}`). The chip-style Tags editor lands in Task 14 and slots into this same section.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 6: Manual smoke**

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit https://watcher.exe.xyz:8001/watched-items/<id>
# Edit the name, save, refresh — value persists.
# Edit interval to "30m", save, confirm child Watch list interval column updates.
```

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/pages/watched_item_detail.html tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem inline-edit fields (name, description, interval, content_type)"
```

---

### Task 14: Tag chip editor

**Files:**
- Create: `src/dashboard/templates/partials/watched_item_tags_editor.html`
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
class TestTagsEditor:
    async def test_get_tags_partial(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="T", default_tags=["a", "b"]
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/tags", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        assert b"a" in response.content and b"b" in response.content

    async def test_add_tag(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="T")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/tags",
            data={"tag": "newtag"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert "newtag" in (wi.default_tags or [])

    async def test_remove_tag(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="T", default_tags=["x", "y", "z"]
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.delete(
            f"/watched-items/{wi.id}/tags/y",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.default_tags == ["x", "z"]

    async def test_add_dedupes(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="T", default_tags=["a"])
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        await client.post(
            f"/watched-items/{wi.id}/tags",
            data={"tag": "a"},
            headers={"HX-Request": "true"},
        )
        await db_session.refresh(wi)
        assert wi.default_tags == ["a"]
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestTagsEditor -v --no-cov -m integration`
Expected: FAIL.

- [ ] **Step 3: Create the chip partial**

`src/dashboard/templates/partials/watched_item_tags_editor.html`:

```html
{# Expects: watched_item. #}
<div id="wi-tags-{{ watched_item.id }}">
  <div class="chip-group">
    {% for t in (watched_item.default_tags or []) %}
    <span class="chip">
      {{ t }}
      <button
        hx-delete="/watched-items/{{ watched_item.id }}/tags/{{ t }}"
        hx-target="#wi-tags-{{ watched_item.id }}"
        hx-swap="outerHTML"
        class="ms-1 text-red-500 hover:text-red-700"
        aria-label="Remove tag {{ t }}">×</button>
    </span>
    {% endfor %}
  </div>
  <form
    hx-post="/watched-items/{{ watched_item.id }}/tags"
    hx-target="#wi-tags-{{ watched_item.id }}"
    hx-swap="outerHTML"
    class="mt-2 flex items-center gap-2">
    <input type="text" name="tag" placeholder="Add tag" class="form-input text-sm"
           required pattern="[^\s,]+" maxlength="50">
    <button type="submit" class="btn btn-secondary btn-sm">Add</button>
  </form>
</div>
```

- [ ] **Step 4: Add the routes**

In `src/dashboard/routes.py`:

```python
@router.get("/watched-items/{watched_item_id}/tags")
async def watched_item_tags_partial(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )


@router.post("/watched-items/{watched_item_id}/tags")
async def watched_item_tag_add(
    request: Request,
    watched_item_id: str,
    tag: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag cannot be empty")
    current = list(wi.default_tags or [])
    if tag not in current:
        current.append(tag)
        wi.default_tags = sorted(current)
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=["default_tags"],
            tag_added=tag,
            source="dashboard",
        )
        await session.commit()
        await session.refresh(wi)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )


@router.delete("/watched-items/{watched_item_id}/tags/{tag}")
async def watched_item_tag_remove(
    request: Request,
    watched_item_id: str,
    tag: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    current = list(wi.default_tags or [])
    if tag in current:
        current.remove(tag)
        wi.default_tags = current or None
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=["default_tags"],
            tag_removed=tag,
            source="dashboard",
        )
        await session.commit()
        await session.refresh(wi)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )
```

- [ ] **Step 5: Mount the editor in the detail template**

Inside the Defaults section of `watched_item_detail.html`, add a Tags row:

```html
<div class="p-4">
  <span class="form-label mb-2 block">Default Tags</span>
  <div
    hx-get="/watched-items/{{ watched_item.id }}/tags"
    hx-trigger="load"
    hx-swap="outerHTML">
    {% include "partials/watched_item_tags_editor.html" %}
  </div>
</div>
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestTagsEditor -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 7: Rebuild CSS + smoke**

```bash
bash scripts/build-css.sh
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit detail page, add tag, remove tag — verify list page chip column updates.
```

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/watched_item_tags_editor.html src/dashboard/templates/pages/watched_item_detail.html src/dashboard/static/css/output.css tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: WatchedItem default-tags chip editor"
```

---

## SLICE 5 — Notification templates + sub_aspect review

### Task 15: Mark-reviewed dashboard route + banner

**Files:**
- Modify: `src/dashboard/context.py` (`count_new_subaspects` helper)
- Modify: `src/dashboard/routes.py` (mark-reviewed POST; pass `new_subaspect_count` to detail context)
- Create: `src/dashboard/templates/partials/watched_item_subaspect_banner.html`
- Test: `tests/dashboard/test_watched_item_routes.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
class TestSubAspectBanner:
    async def test_banner_shows_count_when_new(
        self, client, db_session, info_client
    ):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        old = datetime.now(UTC) - timedelta(days=10)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="Review",
            last_reviewed_at=old,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        info_client.get_info_item = AsyncMock(return_value=SimpleNamespace(
            info_item_id=str(item.info_item_id),
            name="Has new",
            description=None, owner=None,
            info_item_sources=[
                SimpleNamespace(info_source_id="p", role=None,
                                created_at=datetime.now(UTC) - timedelta(days=15)),
                SimpleNamespace(info_source_id="s1", role="sub_aspect",
                                created_at=datetime.now(UTC)),
                SimpleNamespace(info_source_id="s2", role="sub_aspect",
                                created_at=datetime.now(UTC)),
            ],
        ))
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"2 new sub_aspects" in response.content

    async def test_no_banner_when_none_new(
        self, client, db_session, info_client
    ):
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id, name="Reviewed",
            last_reviewed_at=datetime.now(UTC),
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(return_value=SimpleNamespace(
            info_item_id=str(item.info_item_id),
            name="x", description=None, owner=None,
            info_item_sources=[],
        ))
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"new sub_aspects" not in response.content

    async def test_mark_reviewed_stamps_now(
        self, client, db_session, info_client
    ):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item
        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Stamp")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/mark-reviewed", follow_redirects=False
        )
        assert response.status_code in (200, 303)
        await db_session.refresh(wi)
        assert wi.last_reviewed_at is not None
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestSubAspectBanner -v --no-cov -m integration`
Expected: FAIL.

- [ ] **Step 3: Add `count_new_subaspects` helper**

In `src/dashboard/context.py`:

```python
def count_new_subaspects(info_item, last_reviewed_at) -> int:
    """Count sub_aspect bindings created since last_reviewed_at.

    last_reviewed_at=None means all sub_aspects are 'new'.
    """
    if info_item is None:
        return 0
    sources = info_item.info_item_sources or []
    subaspects = [s for s in sources if s.role == "sub_aspect"]
    if last_reviewed_at is None:
        return len(subaspects)
    return sum(1 for s in subaspects if s.created_at > last_reviewed_at)
```

- [ ] **Step 4: Create the banner partial**

`src/dashboard/templates/partials/watched_item_subaspect_banner.html`:

```html
{% if new_subaspect_count and new_subaspect_count > 0 %}
<div class="rounded-md border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/30 p-4 mb-6 flex items-center justify-between">
  <p class="text-sm text-blue-700 dark:text-blue-300">
    <strong>{{ new_subaspect_count }} new sub_aspect{{ "s" if new_subaspect_count != 1 else "" }}</strong>
    available
    {% if watched_item.last_reviewed_at %}since {{ watched_item.last_reviewed_at.strftime('%Y-%m-%d') }}{% endif %}.
  </p>
  <form method="post" action="/watched-items/{{ watched_item.id }}/mark-reviewed">
    <button type="submit"
      hx-post="/watched-items/{{ watched_item.id }}/mark-reviewed"
      hx-target="body" hx-push-url="true"
      class="btn btn-primary btn-sm">Mark reviewed</button>
  </form>
</div>
{% endif %}
```

- [ ] **Step 5: Wire the banner into the detail route + template**

In the detail route, compute and pass:

```python
new_subaspect_count = count_new_subaspects(info_item, wi.last_reviewed_at)
```

Pass `new_subaspect_count` in the context. In `watched_item_detail.html`, include the banner above the Defaults section:

```html
{% include "partials/watched_item_subaspect_banner.html" %}
```

- [ ] **Step 6: Add the mark-reviewed dashboard route**

```python
@router.post("/watched-items/{watched_item_id}/mark-reviewed")
async def watched_item_mark_reviewed(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    from src.api.routes.watched_items import mark_reviewed as _api_mark
    await _api_mark(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(
        url=f"/watched-items/{watched_item_id}", status_code=303
    )
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_routes.py::TestSubAspectBanner -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/context.py src/dashboard/routes.py src/dashboard/templates/pages/watched_item_detail.html src/dashboard/templates/partials/watched_item_subaspect_banner.html tests/dashboard/test_watched_item_routes.py
git commit -m "#161 feat: sub_aspect review banner + mark-reviewed dashboard route"
```

---

### Task 16: Templates list partial + row partial

**Files:**
- Create: `src/dashboard/templates/partials/watched_item_templates.html`
- Create: `src/dashboard/templates/partials/watched_item_template_row.html`
- Modify: `src/dashboard/routes.py` (GET `/partials/watched-item-templates/{id}`)
- Test: `tests/dashboard/test_watched_item_templates.py`

- [ ] **Step 1: Write failing tests**

Create `tests/dashboard/test_watched_item_templates.py`:

```python
"""Integration tests for WatchedItem notification-template UI."""

import pytest

pytestmark = pytest.mark.integration


async def _seed(db_session, name="WI"):
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item
    item = await make_info_item(db_session)
    wi = WatchedItem(info_item_id=item.info_item_id, name=name)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


async def _seed_tpl(db_session, watched_item):
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    tpl = WatchedItemNotificationTemplate(
        watched_item_id=watched_item.id,
        title="Email", channel_hint="mailto://x:y@z",
    )
    db_session.add(tpl)
    await db_session.flush()
    await db_session.commit()
    return tpl


class TestTemplatesPartial:
    async def test_list_empty(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"No notification templates" in response.content

    async def test_list_renders_row(self, client, db_session):
        wi = await _seed(db_session)
        await _seed_tpl(db_session, wi)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"Email" in response.content
        assert b"mailto" in response.content or b"channel" in response.content
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_templates.py::TestTemplatesPartial -v --no-cov -m integration`
Expected: FAIL — 404.

- [ ] **Step 3: Create the partials**

`src/dashboard/templates/partials/watched_item_templates.html`:

```html
{# Expects: watched_item, templates. #}
<table class="data-table" id="wi-templates-table">
  <thead>
    <tr>
      <th>Title</th>
      <th>Channel</th>
      <th>Events</th>
      <th>Status</th>
      <th class="text-end">Actions</th>
    </tr>
  </thead>
  <tbody id="wi-templates-tbody">
    {% if templates %}
      {% for tpl in templates %}
        {% include "partials/watched_item_template_row.html" %}
      {% endfor %}
    {% else %}
      <tr><td colspan="5" class="text-center text-gray-500 py-6">No notification templates yet.</td></tr>
    {% endif %}
  </tbody>
</table>
```

`src/dashboard/templates/partials/watched_item_template_row.html`:

```html
{# Expects: tpl, watched_item. #}
<tr id="wi-tpl-{{ tpl.id }}">
  <td class="font-medium">{{ tpl.title or "—" }}</td>
  <td><span class="chip">{{ tpl.channel_hint }}</span></td>
  <td>
    <div class="chip-group">
    {% for ev in tpl.events %}<span class="chip">{{ ev }}</span>{% endfor %}
    </div>
  </td>
  <td>
    {% if tpl.is_active %}<span class="badge badge-active">Active</span>
    {% else %}<span class="badge badge-inactive">Inactive</span>{% endif %}
  </td>
  <td>
    <div class="flex items-center gap-2 justify-end">
      <button
        hx-get="/watched-items/{{ watched_item.id }}/templates/{{ tpl.id }}/edit"
        hx-target="#wi-tpl-{{ tpl.id }}"
        hx-swap="outerHTML"
        class="btn btn-secondary btn-sm">Edit</button>
      <button
        hx-delete="/watched-items/{{ watched_item.id }}/templates/{{ tpl.id }}"
        hx-confirm="Delete this template? This cannot be undone."
        hx-target="#wi-templates-tbody"
        hx-swap="innerHTML"
        class="btn btn-danger-outline btn-sm">Delete</button>
    </div>
  </td>
</tr>
```

- [ ] **Step 4: Add the partial route**

In `src/dashboard/routes.py`:

```python
@router.get("/partials/watched-item-templates/{watched_item_id}")
async def watched_item_templates_partial(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    from src.dashboard.context import get_watched_item_templates
    templates_ = await get_watched_item_templates(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_templates.html",
        {"watched_item": wi, "templates": templates_},
    )
```

- [ ] **Step 5: Wire into detail template**

In `watched_item_detail.html`, add a Notification Templates section:

```html
<section class="mb-8">
  <div class="flex items-center justify-between mb-4">
    <h3 class="text-lg font-semibold text-gray-900 dark:text-white">Notification Templates</h3>
    <button
      hx-get="/watched-items/{{ watched_item.id }}/templates/new"
      hx-target="#wi-templates-tbody"
      hx-swap="afterbegin"
      class="btn btn-secondary btn-sm">+ Add</button>
  </div>
  <div
    id="wi-templates"
    hx-get="/partials/watched-item-templates/{{ watched_item.id }}"
    hx-trigger="load"
    hx-swap="innerHTML">
    {% include "partials/watched_item_templates.html" %}
  </div>
</section>
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_templates.py::TestTemplatesPartial -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/watched_item_templates.html src/dashboard/templates/partials/watched_item_template_row.html src/dashboard/templates/pages/watched_item_detail.html tests/dashboard/test_watched_item_templates.py
git commit -m "#161 feat: WatchedItem notification-templates table partial"
```

---

### Task 17: Template add/edit/delete dashboard routes

**Files:**
- Create: `src/dashboard/templates/partials/watched_item_template_form.html`
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watched_item_templates.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/dashboard/test_watched_item_templates.py`:

```python
class TestTemplateCrudRoutes:
    async def test_new_form_renders(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/watched-items/{wi.id}/templates/new",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"channel_hint" in response.content

    async def test_create_inserts_row(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.post(
            f"/watched-items/{wi.id}/templates",
            data={"title": "T1", "channel_hint": "mailto://a:b@c", "events": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        listing = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"T1" in listing.content

    async def test_edit_form_renders(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.get(
            f"/watched-items/{wi.id}/templates/{tpl.id}/edit",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Email" in response.content

    async def test_update_persists(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.post(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            data={"title": "Renamed", "channel_hint": tpl.channel_hint, "events": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(tpl)
        assert tpl.title == "Renamed"

    async def test_delete_removes_row(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.delete(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        listing = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"No notification templates" in listing.content
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/dashboard/test_watched_item_templates.py::TestTemplateCrudRoutes -v --no-cov -m integration`
Expected: FAIL — 404s.

- [ ] **Step 3: Create the form partial**

`src/dashboard/templates/partials/watched_item_template_form.html`:

```html
{# Expects: watched_item, tpl (None for new). #}
{% set is_new = tpl is none %}
<tr id="{% if is_new %}wi-tpl-new{% else %}wi-tpl-{{ tpl.id }}{% endif %}">
  <td colspan="5">
    <form
      {% if is_new %}
      hx-post="/watched-items/{{ watched_item.id }}/templates"
      {% else %}
      hx-post="/watched-items/{{ watched_item.id }}/templates/{{ tpl.id }}"
      {% endif %}
      hx-target="#wi-templates-tbody" hx-swap="innerHTML"
      class="space-y-2">
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <input class="form-input" name="title" placeholder="Title (optional)"
               value="{{ tpl.title or '' if not is_new else '' }}">
        <input class="form-input" name="channel_hint" required placeholder="e.g. mailto://x:y@z"
               value="{{ tpl.channel_hint if not is_new else '' }}">
      </div>
      <input class="form-input" name="events" placeholder="Comma-separated events"
             value="{{ (tpl.events|join(',')) if not is_new else 'change_detected' }}">
      <div class="flex gap-2 justify-end">
        <button type="button" hx-get="/partials/watched-item-templates/{{ watched_item.id }}"
                hx-target="#wi-templates" hx-swap="innerHTML"
                class="btn btn-secondary btn-sm">Cancel</button>
        <button type="submit" class="btn btn-primary btn-sm">
          {% if is_new %}Create{% else %}Save{% endif %}
        </button>
      </div>
    </form>
  </td>
</tr>
```

- [ ] **Step 4: Add routes**

In `src/dashboard/routes.py`:

```python
@router.get("/watched-items/{watched_item_id}/templates/new")
async def watched_item_template_new_form(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_form.html",
        {"watched_item": wi, "tpl": None},
    )


@router.post("/watched-items/{watched_item_id}/templates")
async def watched_item_template_create(
    request: Request,
    watched_item_id: str,
    title: str = Form(""),
    channel_hint: str = Form(...),
    events: str = Form("change_detected"),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    try:
        event_list = validate_event_list(event_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tpl = WatchedItemNotificationTemplate(
        watched_item_id=wi.id,
        title=title.strip() or None,
        channel_hint=channel_hint.strip(),
        events=event_list,
    )
    session.add(tpl)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_CREATED,
        watched_item_id=str(wi.id),
        source="dashboard",
    )
    await session.commit()

    from src.dashboard.context import get_watched_item_templates
    refreshed = await get_watched_item_templates(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_templates.html",
        {"watched_item": wi, "templates": refreshed},
    )


@router.get("/watched-items/{watched_item_id}/templates/{tpl_id}/edit")
async def watched_item_template_edit_form(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    tpl = await session.get(
        WatchedItemNotificationTemplate, parse_ulid(tpl_id)
    )
    if not tpl or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_form.html",
        {"watched_item": wi, "tpl": tpl},
    )


@router.post("/watched-items/{watched_item_id}/templates/{tpl_id}")
async def watched_item_template_update(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    title: str = Form(""),
    channel_hint: str = Form(...),
    events: str = Form("change_detected"),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    tpl = await session.get(
        WatchedItemNotificationTemplate, parse_ulid(tpl_id)
    )
    if not tpl or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404)
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    try:
        event_list = validate_event_list(event_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tpl.title = title.strip() or None
    tpl.channel_hint = channel_hint.strip()
    tpl.events = event_list
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_UPDATED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="dashboard",
    )
    await session.commit()

    from src.dashboard.context import get_watched_item_templates
    refreshed = await get_watched_item_templates(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_templates.html",
        {"watched_item": wi, "templates": refreshed},
    )


@router.delete("/watched-items/{watched_item_id}/templates/{tpl_id}")
async def watched_item_template_delete(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    tpl = await session.get(
        WatchedItemNotificationTemplate, parse_ulid(tpl_id)
    )
    if not tpl or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_DELETED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="dashboard",
    )
    await session.delete(tpl)
    await session.commit()

    from src.dashboard.context import get_watched_item_templates
    refreshed = await get_watched_item_templates(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_templates.html",
        {"watched_item": wi, "templates": refreshed},
    )
```

Move all inline imports to the top of `routes.py` per project convention.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/dashboard/test_watched_item_templates.py -v --no-cov -m integration`
Expected: PASS.

- [ ] **Step 6: Manual smoke**

```bash
bash scripts/build-css.sh
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit a WatchedItem detail page. Add a template, edit it, delete it.
# Trigger a watch run (or wait for schedule_tick) and confirm the notifier
# receives a dispatch carrying the new template.
```

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/watched_item_template_form.html src/dashboard/static/css/output.css tests/dashboard/test_watched_item_templates.py
git commit -m "#161 feat: WatchedItem notification-template add/edit/delete (dashboard)"
```

---

## Final verification

### Task 18: Lift inline imports + lint

`src/dashboard/routes.py` currently has zero inline imports (verified). Tasks 9, 10, 13, 15, 16, 17 each suggested moving inline imports to the top, but an implementer working task-by-task may commit each task with its inline imports in place. This task is a single deliberate cleanup pass.

**Files:**
- Modify: `src/dashboard/routes.py`

- [ ] **Step 1: Identify accumulated inline imports**

Run:

```bash
grep -n "^    from \|^        from " /home/exedev/watcher/src/dashboard/routes.py
```

Expected matches (lift each to the top of the file, into the existing import block, sorted with ruff conventions):

- `from src.dashboard.context import get_watched_item_detail` (used by Tasks 13, 14, 15, 16, 17)
- `from src.dashboard.context import get_watched_item_templates` (Tasks 16, 17)
- `from src.core.models.watch import Watch` (Task 9 — for the child-watches query)
- `from src.core.models.watched_item_notification_template import WatchedItemNotificationTemplate` (Task 17)
- `from src.core.scheduler import parse_interval` (Task 11 — may already be top-level if added correctly)
- `from src.api.routes.watched_items import archive_watched_item, restore_watched_item, mark_reviewed` (Tasks 10, 15) — alias each on import to avoid name collisions with the dashboard wrappers (e.g. `archive_watched_item as _api_archive_watched_item`).
- `from src.api.schemas.validators import validate_event_list` — confirm this is already imported at top (line 22); if so, no action.

- [ ] **Step 2: Remove every inline import**

After lifting, no `def`-scope `from`/`import` statements should remain in `src/dashboard/routes.py`.

Confirm:

```bash
grep -n "^    from \|^        from \|^    import \|^        import " /home/exedev/watcher/src/dashboard/routes.py
```
Expected: empty.

- [ ] **Step 3: Lint**

```bash
uv run ruff check src/dashboard/routes.py
```
Expected: clean (no `F401` unused imports, no `E402` module-level import not at top).

- [ ] **Step 4: Run dashboard tests to confirm no regressions**

```bash
uv run pytest tests/dashboard/test_watched_item_routes.py tests/dashboard/test_watched_item_templates.py -v --no-cov -m integration 2>&1 | tail -10
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py
git commit -m "#161 refactor: lift WatchedItem inline imports to module top"
```

---

### Task 19: Full test sweep + smoke + close

- [ ] **Step 1: Full unit run**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -5
```
Expected: PASS, no regressions in `tests/core/` or `tests/api/schemas/`.

- [ ] **Step 2: Full integration run**

```bash
uv run pytest --no-cov -m integration 2>&1 | tail -5
```
Expected: PASS.

- [ ] **Step 3: Repo-wide lint**

```bash
uv run ruff check .
```
Expected: no errors.

- [ ] **Step 4: Final smoke — dev server first, then production restart**

Dev (port 8001) before prod (port 8000) per [AGENTS.md](../../AGENTS.md) lifecycle rules:

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit https://watcher.exe.xyz:8001/watched-items
```

Verify on dev: list page loads, detail page loads with InfoItem summary, defaults editor saves, tag chip add/remove works, template CRUD works, archive cascades, restore is parent-only, sub_aspect banner appears.

Once dev smoke passes and changes are on `main`:

```bash
sudo systemctl restart watcher
sudo journalctl -u watcher -f
# Visit https://watcher.exe.xyz/watched-items
```

- [ ] **Step 5: Close the issue**

```bash
gh issue close 161 --comment "Shipped via the WatchedItem CRUD UI implementation plan. Follow-up #164 remains open for the bulk 'Add Watches for new sub_aspects' action."
```

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `reduce_frequency` post-action races with operator interval edit | Low | Documented in field hint; reduce_frequency wins (last write). Operator can re-edit. |
| Archiver service down → detail page 500s | Medium | Route catches `NotFound`; should also catch `httpx.ConnectError` and degrade gracefully (render page with "InfoItem summary unavailable" placeholder). Add to Task 9 if it surfaces in smoke testing. |
| Cascade archive surprises operators (children silently archived) | Low | `hx-confirm` text explicitly states cascade behavior. |
| Tag chip URL-path encoding (tags with special chars) | Low | `pattern="[^\s,]+"` in the form prevents whitespace/comma; FastAPI handles URL-encoding for path params automatically. |
| Slice 4 schema-config edit conflicts with the `reduce_frequency` field hint copy | Low | Field hint is in `WATCHED_ITEM_FIELD_META`, single source of truth. |
| `validate_event_list` rejects new event types added later | Low | It already returns a curated list per [src/api/schemas/validators.py](../../src/api/schemas/validators.py). Adding new events is a separate, intentional change. |

---

## Out of scope (do not implement in this plan)

- Bulk "Add Watches for new sub_aspects" — #164.
- InfoItem typeahead picker on Watch-create — separate follow-up.
- Cross-InfoItem Collection grouping — descoped from #160.
- Free-form JSONB editor for `default_schedule_config`.
- Per-Watch suppression of inherited WatchedItem templates.
- Standalone `POST /api/v1/watched-items` create endpoint.
