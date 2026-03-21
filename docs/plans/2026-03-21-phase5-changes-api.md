# Phase 5: Changes & Audit Log API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add query API endpoints for changes (with filters by watch, date range, content type) and audit log entries, with structured change metadata in responses.

**Architecture:** New Pydantic response schemas for Change, Snapshot, SnapshotChunk, and AuditLog. New route modules with query endpoints supporting pagination and filtering. No model changes — all data structures exist from Phases 2-3.

**Tech Stack:** FastAPI, SQLAlchemy async queries, Pydantic v2

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  api/
    schemas/
      change.py      — create: ChangeResponse, SnapshotResponse, SnapshotChunkResponse
      audit_log.py   — create: AuditLogResponse
    routes/
      changes.py     — create: list changes, get change detail
      audit_log.py   — create: list audit log entries
    main.py          — modify: include new routers
tests/
  api/
    test_changes.py    — create: integration tests for changes API
    test_audit_log.py  — create: integration tests for audit log API
AGENTS.md              — modify: add new routes to layout
```

---

## Task 1: Change and Snapshot response schemas

**Files:**
- Create: `src/api/schemas/change.py`
- Test: `tests/api/test_schemas.py` (add to existing)

- [ ] **Step 1: Write failing tests**

Add to `tests/api/test_schemas.py`:

```python
from datetime import UTC, datetime

from src.api.schemas.change import (
    ChangeResponse,
    SnapshotChunkResponse,
    SnapshotResponse,
)


class TestSnapshotChunkResponse:
    def test_from_dict(self):
        data = {
            "id": "01HXYZ",
            "snapshot_id": "01HABC",
            "chunk_index": 0,
            "chunk_type": "page",
            "chunk_label": "Page 1",
            "content_hash": "abc123",
            "simhash": 12345,
            "char_count": 500,
            "excerpt": "First page content...",
        }
        schema = SnapshotChunkResponse.model_validate(data)
        assert schema.chunk_index == 0
        assert schema.chunk_type == "page"


class TestSnapshotResponse:
    def test_from_dict(self):
        now = datetime.now(UTC)
        data = {
            "id": "01HABC",
            "watch_id": "01HWAT",
            "content_hash": "abc123",
            "simhash": 12345,
            "storage_path": "snapshots/01HWAT/01HABC.html",
            "text_path": "snapshots/01HWAT/01HABC.txt",
            "storage_backend": "local",
            "chunk_count": 3,
            "text_bytes": 1500,
            "fetch_duration_ms": 200,
            "fetcher_used": "http",
            "fetched_at": now,
        }
        schema = SnapshotResponse.model_validate(data)
        assert schema.chunk_count == 3
        assert schema.fetcher_used == "http"


class TestChangeResponse:
    def test_from_dict(self):
        now = datetime.now(UTC)
        data = {
            "id": "01HCHG",
            "watch_id": "01HWAT",
            "previous_snapshot_id": "01HPREV",
            "current_snapshot_id": "01HCURR",
            "change_metadata": {
                "added": ["Page 3"],
                "removed": [],
                "modified": [{"label": "Page 1", "similarity": 0.92}],
            },
            "detected_at": now,
        }
        schema = ChangeResponse.model_validate(data)
        assert len(schema.change_metadata["modified"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_schemas.py::TestSnapshotChunkResponse -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement schemas**

Create `src/api/schemas/change.py`:

```python
"""Pydantic schemas for Change, Snapshot, and SnapshotChunk responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.api.schemas.types import ULIDStr


class SnapshotChunkResponse(BaseModel):
    """Response schema for a snapshot chunk."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    snapshot_id: ULIDStr
    chunk_index: int
    chunk_type: str
    chunk_label: str
    content_hash: str
    simhash: int
    char_count: int
    excerpt: str


class SnapshotResponse(BaseModel):
    """Response schema for a snapshot."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    content_hash: str
    simhash: int
    storage_path: str
    text_path: str
    storage_backend: str
    chunk_count: int
    text_bytes: int
    fetch_duration_ms: int
    fetcher_used: str
    fetched_at: datetime


class ChangeResponse(BaseModel):
    """Response schema for a detected change."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    previous_snapshot_id: ULIDStr
    current_snapshot_id: ULIDStr
    change_metadata: dict
    detected_at: datetime
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
uv run pytest tests/api/test_schemas.py -v
uv run ruff check .
git add src/api/schemas/change.py tests/api/test_schemas.py
git commit -m "#2 feat: add Change, Snapshot, SnapshotChunk response schemas"
```

---

## Task 2: AuditLog response schema

**Files:**
- Create: `src/api/schemas/audit_log.py`
- Modify: `tests/api/test_schemas.py`

- [ ] **Step 1: Write failing test**

```python
from src.api.schemas.audit_log import AuditLogResponse


class TestAuditLogResponse:
    def test_from_dict(self):
        now = datetime.now(UTC)
        data = {
            "id": "01HAUD",
            "event_type": "check.snapshot_created",
            "watch_id": "01HWAT",
            "payload": {"snapshot_id": "01HSNAP", "is_changed": True},
            "created_at": now,
        }
        schema = AuditLogResponse.model_validate(data)
        assert schema.event_type == "check.snapshot_created"
        assert schema.payload["is_changed"] is True

    def test_nullable_watch_id(self):
        now = datetime.now(UTC)
        data = {
            "id": "01HAUD",
            "event_type": "system.startup",
            "watch_id": None,
            "payload": {},
            "created_at": now,
        }
        schema = AuditLogResponse.model_validate(data)
        assert schema.watch_id is None
```

- [ ] **Step 2: Implement schema**

Create `src/api/schemas/audit_log.py`:

```python
"""Pydantic schema for AuditLog responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.api.schemas.types import ULIDStr


class AuditLogResponse(BaseModel):
    """Response schema for an audit log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    event_type: str
    watch_id: ULIDStr | None
    payload: dict
    created_at: datetime
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
uv run pytest tests/api/test_schemas.py -v
uv run ruff check .
git add src/api/schemas/audit_log.py tests/api/test_schemas.py
git commit -m "#2 feat: add AuditLog response schema"
```

---

## Task 3: Changes API routes

**Files:**
- Create: `src/api/routes/changes.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_changes.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/api/test_changes.py`:

```python
"""Integration tests for changes API endpoints."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch

pytestmark = pytest.mark.integration


@pytest.fixture
async def watch_with_changes(db_session: AsyncSession):
    """Create a watch with two snapshots and a change record."""
    from src.core.models.base import generate_ulid

    watch = Watch(
        name="Test Watch",
        url="https://example.com",
        content_type=ContentType.HTML,
    )
    db_session.add(watch)
    await db_session.flush()

    snap1_id = generate_ulid()
    snap1 = Snapshot(
        id=snap1_id,
        watch_id=watch.id,
        content_hash="aaa",
        simhash=100,
        storage_path="s/1.html",
        text_path="s/1.txt",
        storage_backend="local",
        chunk_count=1,
        text_bytes=100,
        fetch_duration_ms=50,
        fetcher_used="http",
    )
    db_session.add(snap1)

    snap2_id = generate_ulid()
    snap2 = Snapshot(
        id=snap2_id,
        watch_id=watch.id,
        content_hash="bbb",
        simhash=200,
        storage_path="s/2.html",
        text_path="s/2.txt",
        storage_backend="local",
        chunk_count=2,
        text_bytes=200,
        fetch_duration_ms=60,
        fetcher_used="http",
    )
    db_session.add(snap2)
    await db_session.flush()

    # Add chunks to snap2
    for i in range(2):
        db_session.add(SnapshotChunk(
            snapshot_id=snap2_id,
            chunk_index=i,
            chunk_type="section",
            chunk_label=f"Section {i + 1}",
            content_hash=f"chunk{i}",
            simhash=300 + i,
            char_count=100,
            excerpt=f"Excerpt for section {i + 1}...",
        ))

    change = Change(
        watch_id=watch.id,
        previous_snapshot_id=snap1_id,
        current_snapshot_id=snap2_id,
        change_metadata={
            "added": ["Section 2"],
            "removed": [],
            "modified": [{"label": "Section 1", "similarity": 0.95}],
        },
    )
    db_session.add(change)
    await db_session.commit()

    return {"watch": watch, "snap1": snap1, "snap2": snap2, "change": change}


class TestListChanges:
    async def test_list_all_changes(self, client, watch_with_changes):
        response = await client.get("/api/changes")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1

    async def test_filter_by_watch_id(self, client, watch_with_changes):
        watch_id = str(watch_with_changes["watch"].id)
        response = await client.get(f"/api/changes?watch_id={watch_id}")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["watch_id"] == watch_id

    async def test_filter_by_nonexistent_watch(self, client, watch_with_changes):
        response = await client.get(
            "/api/changes?watch_id=00000000000000000000000000"
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_pagination(self, client, watch_with_changes):
        response = await client.get("/api/changes?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1


class TestGetChangeDetail:
    async def test_get_change_with_chunks(self, client, watch_with_changes):
        change_id = str(watch_with_changes["change"].id)
        response = await client.get(f"/api/changes/{change_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == change_id
        assert "current_snapshot" in data
        assert "chunks" in data["current_snapshot"]
        assert len(data["current_snapshot"]["chunks"]) == 2

    async def test_get_nonexistent_change(self, client):
        response = await client.get(
            "/api/changes/00000000000000000000000000"
        )
        assert response.status_code == 404
```

- [ ] **Step 2: Implement routes**

Create `src/api/routes/changes.py`:

```python
"""Changes API endpoints — query detected changes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.change import (
    ChangeResponse,
    SnapshotChunkResponse,
    SnapshotResponse,
)
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk

router = APIRouter(prefix="/api/changes", tags=["changes"])


def _parse_ulid(value: str) -> ULID:
    """Parse ULID string, raising 404 on invalid format."""
    try:
        return ULID.from_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


@router.get("", response_model=list[ChangeResponse])
async def list_changes(
    watch_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    """List changes with optional watch_id filter and pagination."""
    stmt = select(Change).order_by(Change.detected_at.desc())
    if watch_id:
        stmt = stmt.where(Change.watch_id == _parse_ulid(watch_id))
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


class ChangeDetailResponse(ChangeResponse):
    """Change with embedded snapshot and chunk details."""

    current_snapshot: SnapshotWithChunksResponse | None = None
    previous_snapshot: SnapshotWithChunksResponse | None = None


class SnapshotWithChunksResponse(SnapshotResponse):
    """Snapshot with embedded chunks."""

    chunks: list[SnapshotChunkResponse] = []


@router.get("/{change_id}", response_model=ChangeDetailResponse)
async def get_change_detail(
    change_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Get a change with its snapshots and chunk details."""
    change = await session.get(Change, _parse_ulid(change_id))
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    # Build response with snapshots and chunks
    response_data = {
        "id": change.id,
        "watch_id": change.watch_id,
        "previous_snapshot_id": change.previous_snapshot_id,
        "current_snapshot_id": change.current_snapshot_id,
        "change_metadata": change.change_metadata,
        "detected_at": change.detected_at,
    }

    for snapshot_key, snapshot_id in [
        ("current_snapshot", change.current_snapshot_id),
        ("previous_snapshot", change.previous_snapshot_id),
    ]:
        snap = await session.get(Snapshot, snapshot_id)
        if snap:
            chunk_stmt = (
                select(SnapshotChunk)
                .where(SnapshotChunk.snapshot_id == snapshot_id)
                .order_by(SnapshotChunk.chunk_index)
            )
            chunk_result = await session.execute(chunk_stmt)
            chunks = chunk_result.scalars().all()
            snap_data = SnapshotWithChunksResponse.model_validate(snap)
            snap_data.chunks = [
                SnapshotChunkResponse.model_validate(c) for c in chunks
            ]
            response_data[snapshot_key] = snap_data

    return ChangeDetailResponse.model_validate(response_data)
```

NOTE: The `ChangeDetailResponse` and `SnapshotWithChunksResponse` classes are defined in the routes file since they're endpoint-specific compositions of the base schemas. Move to schemas if reused elsewhere.

- [ ] **Step 3: Include router in main.py**

Add to `src/api/main.py`:
```python
from src.api.routes.changes import router as changes_router
app.include_router(changes_router)
```

- [ ] **Step 4: Run integration tests**

```bash
uv run pytest tests/api/test_changes.py -v -m integration
```

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add src/api/routes/changes.py src/api/main.py tests/api/test_changes.py
git commit -m "#2 feat: add changes API (list with filters, detail with snapshots)"
```

---

## Task 4: Audit log API routes

**Files:**
- Create: `src/api/routes/audit_log.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_audit_log.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/api/test_audit_log.py`:

```python
"""Integration tests for audit log API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestListAuditLog:
    async def test_list_audit_entries(self, client):
        """Creating a watch generates audit entries — list should return them."""
        await client.post("/api/watches", json={
            "name": "Audit Test",
            "url": "https://example.com",
            "content_type": "html",
        })
        response = await client.get("/api/audit")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(e["event_type"] == "watch.created" for e in data)

    async def test_filter_by_event_type(self, client):
        await client.post("/api/watches", json={
            "name": "Event Filter",
            "url": "https://example.com",
            "content_type": "html",
        })
        response = await client.get("/api/audit?event_type=watch.created")
        assert response.status_code == 200
        data = response.json()
        assert all(e["event_type"] == "watch.created" for e in data)

    async def test_filter_by_watch_id(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Watch Filter",
            "url": "https://example.com",
            "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/api/audit?watch_id={watch_id}")
        assert response.status_code == 200
        data = response.json()
        assert all(e["watch_id"] == watch_id for e in data)

    async def test_pagination(self, client):
        response = await client.get("/api/audit?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1
```

- [ ] **Step 2: Implement routes**

Create `src/api/routes/audit_log.py`:

```python
"""Audit log API endpoints — query system event history."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.audit_log import AuditLogResponse
from src.core.models.audit_log import AuditLog

router = APIRouter(prefix="/api/audit", tags=["audit-log"])


@router.get("", response_model=list[AuditLogResponse])
async def list_audit_entries(
    event_type: str | None = Query(None),
    watch_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    """List audit log entries with optional filters and pagination."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if watch_id:
        try:
            parsed = ULID.from_str(watch_id)
        except ValueError:
            return []
        stmt = stmt.where(AuditLog.watch_id == parsed)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()
```

- [ ] **Step 3: Include router in main.py**

Add to `src/api/main.py`:
```python
from src.api.routes.audit_log import router as audit_router
app.include_router(audit_router)
```

- [ ] **Step 4: Run integration tests**

```bash
uv run pytest tests/api/test_audit_log.py -v -m integration
```

- [ ] **Step 5: Commit**

```bash
uv run ruff check .
git add src/api/routes/audit_log.py src/api/main.py tests/api/test_audit_log.py
git commit -m "#2 feat: add audit log API (list with event_type and watch_id filters)"
```

---

## Task 5: Documentation

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update project layout**

Add new route files to the layout section in AGENTS.md.

- [ ] **Step 2: Run full suite, lint, commit**

```bash
uv run pytest -v
uv run pytest -m integration -v
uv run ruff check .
git add AGENTS.md
git commit -m "#2 docs: add changes and audit log API to project layout"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Change/Snapshot/SnapshotChunk response schemas | ~3 unit |
| 2 | AuditLog response schema | ~2 unit |
| 3 | Changes API (list + detail with embedded snapshots/chunks) | ~5 integration |
| 4 | Audit log API (list with filters) | ~4 integration |
| 5 | Documentation | — |

Total: ~14 new automated tests
