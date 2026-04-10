# Notification Template Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a shared Notification Configuration (NC) template library with watch/domain auto-assignment, a `/notifications` management UI, and redesigned Watch/Domain NC sections with library vs. local distinction.

**Architecture:** New `notification_templates` table (library) + two junction tables (`watch_nc_refs`, `domain_nc_refs`). Rename `notification_configs` → `watch_notification_configs`. Dispatch unions local configs and template refs. Auto-assignment fires on Watch create from global/domain defaults. Top-level `/notifications` page owns template CRUD; Watch detail gains Library/Local groups; Domain detail gains an NC defaults section.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL (ARRAY, FK), Jinja2 + HTMX, Apprise, Fernet encryption (cryptography), pytest (unit + integration)

---

## File Map

**New files:**
- `alembic/versions/XXXX_notification_template_library.py` — migration
- `src/core/models/notification_template.py` — `NotificationTemplate`, `WatchNcRef`, `DomainNcRef`
- `src/api/schemas/notification_template.py` — Pydantic request/response schemas
- `src/api/routes/notification_templates.py` — REST CRUD + test endpoint for templates
- `src/dashboard/templates/pages/notifications.html` — template library page
- `src/dashboard/templates/partials/notification_template_row.html` — single template table row partial
- `src/dashboard/templates/partials/notification_template_add_row.html` — inline add-row form
- `src/dashboard/templates/partials/notification_template_edit_form.html` — inline edit form
- `src/dashboard/templates/partials/watch_nc_assign_row.html` — assign-from-library add-row form
- `tests/api/test_notification_templates.py` — integration tests for template CRUD API
- `tests/workers/test_notify_templates.py` — unit tests for dispatch union

**Modified files:**
- `src/core/models/notification_config.py` — rename class + `__tablename__`
- `src/core/models/__init__.py` — add new exports, rename `NotificationConfig` → `WatchNotificationConfig`
- `src/core/models/audit_log.py` — add new `EventType` constants
- `src/workers/notify.py` — union dispatch + `DispatchCandidate` dataclass
- `src/api/routes/notification_configs.py` — update import of renamed class
- `src/api/main.py` — mount `notification_templates_router`
- `src/dashboard/routes.py` — `/notifications` page routes, domain NC default routes, watch NC assign/unassign/copy routes; update `NotificationConfig` → `WatchNotificationConfig` import
- `src/dashboard/templates/base.html` — add Notifications nav link (desktop + mobile)
- `src/dashboard/templates/partials/watch_notifications.html` — Library/Local visual groups + new actions
- `src/dashboard/templates/partials/notification_add_row.html` — split Add into "local" vs "from library"

---

## Task 1: Database Migration

**Files:**
- Create: `alembic/versions/XXXX_notification_template_library.py`

- [ ] **Step 1: Write the migration**

Run `export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)` first, then create the file manually. Use `down_revision = "6dbe1199d3a0"`. Generate a revision ID with:
```bash
python -c "import secrets; print(secrets.token_hex(10))"
```

Full migration content:

```python
"""notification template library

Revision ID: <generated>
Revises: 6dbe1199d3a0
Create Date: 2026-04-10
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "<generated>"
down_revision: Union[str, Sequence[str], None] = "6dbe1199d3a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename existing table
    op.rename_table("notification_configs", "watch_notification_configs")

    # 2. Create notification_templates
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("apprise_url", sa.Text(), nullable=False),
        sa.Column("channel_hint", sa.String(50), nullable=False),
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(50)),
            nullable=False,
            server_default=sa.text("ARRAY['change_detected']::varchar[]"),
        ),
        sa.Column("is_global_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # 3. Create watch_nc_refs junction
    op.create_table(
        "watch_nc_refs",
        sa.Column("watch_id", sa.String(26), nullable=False),
        sa.Column("template_id", sa.String(26), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["watch_id"], ["watches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["notification_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("watch_id", "template_id"),
    )

    # 4. Create domain_nc_refs junction
    op.create_table(
        "domain_nc_refs",
        sa.Column("domain_name", sa.String(253), nullable=False),
        sa.Column("template_id", sa.String(26), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["domain_name"], ["domains.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["notification_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("domain_name", "template_id"),
    )


def downgrade() -> None:
    op.drop_table("domain_nc_refs")
    op.drop_table("watch_nc_refs")
    op.drop_table("notification_templates")
    op.rename_table("watch_notification_configs", "notification_configs")
```

- [ ] **Step 2: Apply and verify**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head
```

Expected: `Running upgrade 6dbe1199d3a0 -> <revision>, notification template library`

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
```

Both should succeed cleanly.

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/
git commit -m "#85 feat: migration — notification_template_library tables"
```

---

## Task 2: New SQLAlchemy Models + Rename

**Files:**
- Create: `src/core/models/notification_template.py`
- Modify: `src/core/models/notification_config.py`
- Modify: `src/core/models/__init__.py`

- [ ] **Step 1: Write failing import test**

```python
# tests/test_notification_template_model.py
def test_imports():
    from src.core.models import (
        WatchNotificationConfig,
        NotificationTemplate,
        WatchNcRef,
        DomainNcRef,
    )
    assert WatchNotificationConfig.__tablename__ == "watch_notification_configs"
    assert NotificationTemplate.__tablename__ == "notification_templates"
    assert WatchNcRef.__tablename__ == "watch_nc_refs"
    assert DomainNcRef.__tablename__ == "domain_nc_refs"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
uv run pytest tests/test_notification_template_model.py -v
```

Expected: `ImportError` or `AttributeError`

- [ ] **Step 3: Rename NotificationConfig class and tablename**

In `src/core/models/notification_config.py`, change:
```python
# Before
class NotificationConfig(TimestampMixin, Base):
    __tablename__ = "notification_configs"

# After
class WatchNotificationConfig(TimestampMixin, Base):
    __tablename__ = "watch_notification_configs"
```

No other changes to this file.

- [ ] **Step 4: Create notification_template.py**

```python
"""Notification template library models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from ulid import ULID


class NotificationTemplate(TimestampMixin, Base):
    """Shared, reusable notification configuration template."""

    __tablename__ = "notification_templates"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    apprise_url: Mapped[str] = mapped_column(Text, nullable=False)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        server_default=text("ARRAY['change_detected']::varchar[]"),
    )
    is_global_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class WatchNcRef(Base):
    """Junction: NotificationTemplate assigned to a Watch."""

    __tablename__ = "watch_nc_refs"

    watch_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("watches.id", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("notification_templates.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DomainNcRef(Base):
    """Junction: NotificationTemplate that is a default for a Domain."""

    __tablename__ = "domain_nc_refs"

    domain_name: Mapped[str] = mapped_column(
        String(253), ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[ULID] = mapped_column(
        ULIDType, ForeignKey("notification_templates.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 5: Update `__init__.py`**

Add to `src/core/models/__init__.py`:
- Replace `NotificationConfig` export with `WatchNotificationConfig`
- Add `NotificationTemplate`, `WatchNcRef`, `DomainNcRef` exports
- Add the import lines for new models

- [ ] **Step 6: Run test to confirm it passes**

```bash
uv run pytest tests/test_notification_template_model.py -v
```

Expected: PASS

- [ ] **Step 7: Update all existing references to the old class name**

Search for every import and usage of `NotificationConfig` and replace with `WatchNotificationConfig`:

```bash
grep -rn "NotificationConfig" src/ tests/ --include="*.py"
```

Files to update:
- `src/api/routes/notification_configs.py` — import + type annotations
- `src/dashboard/routes.py` — import + usages
- `src/workers/notify.py` — import + usages
- Any other files found by grep

- [ ] **Step 8: Run full unit test suite**

```bash
uv run pytest --no-cov -m "not integration" -q
```

Expected: all pass (same count as baseline)

- [ ] **Step 9: Commit**

```bash
git add src/core/models/ tests/test_notification_template_model.py
git commit -m "#85 feat: add NotificationTemplate/WatchNcRef/DomainNcRef models, rename WatchNotificationConfig"
```

---

## Task 3: New Audit Event Types

**Files:**
- Modify: `src/core/models/audit_log.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_audit_event_types.py
def test_new_event_types_exist():
    from src.core.models.audit_log import EventType

    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_CREATED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_UPDATED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_DELETED")
    assert hasattr(EventType, "NOTIFICATION_TEMPLATE_TESTED")
    assert hasattr(EventType, "WATCH_NC_ASSIGNED")
    assert hasattr(EventType, "WATCH_NC_UNASSIGNED")
    assert hasattr(EventType, "DOMAIN_NC_DEFAULT_ADDED")
    assert hasattr(EventType, "DOMAIN_NC_DEFAULT_REMOVED")
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_audit_event_types.py -v
```

Expected: `AttributeError`

- [ ] **Step 3: Add constants to EventType in audit_log.py**

In the `EventType` class (or wherever the string constants are defined), add:

```python
NOTIFICATION_TEMPLATE_CREATED = "notification_template_created"
NOTIFICATION_TEMPLATE_UPDATED = "notification_template_updated"
NOTIFICATION_TEMPLATE_DELETED = "notification_template_deleted"
NOTIFICATION_TEMPLATE_TESTED = "notification_template_tested"
WATCH_NC_ASSIGNED = "watch_nc_assigned"
WATCH_NC_UNASSIGNED = "watch_nc_unassigned"
DOMAIN_NC_DEFAULT_ADDED = "domain_nc_default_added"
DOMAIN_NC_DEFAULT_REMOVED = "domain_nc_default_removed"
```

- [ ] **Step 4: Run to confirm pass**

```bash
uv run pytest tests/test_audit_event_types.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/models/audit_log.py tests/test_audit_event_types.py
git commit -m "#85 feat: add template/assignment audit event types"
```

---

## Task 4: Dispatch Union

**Files:**
- Modify: `src/workers/notify.py`
- Create: `tests/workers/test_notify_templates.py`

The current `dispatch_event_notifications` queries only `WatchNotificationConfig`. It must also pull `NotificationTemplate` rows joined via `WatchNcRef`. A `DispatchCandidate` dataclass unifies both result types.

- [ ] **Step 1: Write failing unit tests**

```python
# tests/workers/test_notify_templates.py
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from datetime import datetime, timezone

from src.core.notifications.events import WatchEvent, WatchEventType


def _make_event():
    return WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id="01J000000000000000000000AA",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_dispatch_includes_template_refs():
    """Templates assigned to the watch via watch_nc_refs are dispatched."""
    from src.workers.notify import dispatch_event_notifications

    event = _make_event()

    # Simulate: no local configs, one template ref
    mock_local = MagicMock()
    mock_local.scalars.return_value.all.return_value = []

    mock_template = MagicMock()
    fake_template = MagicMock()
    fake_template.id = "01J000000000000000000000BB"
    fake_template.apprise_url = "json://hooks.example.com/notify"
    mock_template.scalars.return_value.all.return_value = [fake_template]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[mock_local, mock_template])

    with patch("src.workers.notify.dispatch_event", new_callable=AsyncMock) as mock_dispatch:
        mock_dispatch.return_value = MagicMock(success=True, reason="ok")
        await dispatch_event_notifications(session, event)

    mock_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_includes_both_local_and_template():
    """Both local configs and template refs are dispatched."""
    from src.workers.notify import dispatch_event_notifications

    event = _make_event()

    mock_local = MagicMock()
    fake_local = MagicMock()
    fake_local.id = "01J000000000000000000000CC"
    fake_local.apprise_url = "json://local.example.com/notify"
    mock_local.scalars.return_value.all.return_value = [fake_local]

    mock_template = MagicMock()
    fake_template = MagicMock()
    fake_template.id = "01J000000000000000000000DD"
    fake_template.apprise_url = "json://template.example.com/notify"
    mock_template.scalars.return_value.all.return_value = [fake_template]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[mock_local, mock_template])

    with patch("src.workers.notify.dispatch_event", new_callable=AsyncMock) as mock_dispatch:
        mock_dispatch.return_value = MagicMock(success=True, reason="ok")
        await dispatch_event_notifications(session, event)

    assert mock_dispatch.call_count == 2
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/workers/test_notify_templates.py -v
```

Expected: FAIL (dispatch only calls once for local)

- [ ] **Step 3: Update notify.py**

Add `DispatchCandidate` dataclass and update `dispatch_event_notifications`:

```python
# Add at top of file (imports section):
from dataclasses import dataclass

from src.core.models.notification_template import NotificationTemplate, WatchNcRef

@dataclass
class DispatchCandidate:
    apprise_url: str
    source: str  # "local" | "template"
    source_id: str
```

Replace the query + loop in `dispatch_event_notifications`:

```python
async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    watch_ulid = ULID.from_str(event.watch_id)

    # 1. Local watch_notification_configs
    local_result = await session.execute(
        select(WatchNotificationConfig)
        .where(
            WatchNotificationConfig.watch_id == watch_ulid,
            WatchNotificationConfig.is_active.is_(True),
            WatchNotificationConfig.events.contains([event.event_type.value]),
        )
    )
    local_configs = local_result.scalars().all()

    # 2. Template refs via watch_nc_refs
    template_result = await session.execute(
        select(NotificationTemplate)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(
            WatchNcRef.watch_id == watch_ulid,
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event.event_type.value]),
        )
    )
    templates = template_result.scalars().all()

    candidates: list[DispatchCandidate] = [
        DispatchCandidate(apprise_url=c.apprise_url, source="local", source_id=str(c.id))
        for c in local_configs
    ] + [
        DispatchCandidate(apprise_url=t.apprise_url, source="template", source_id=str(t.id))
        for t in templates
    ]

    if not candidates:
        return

    results = []
    for candidate in candidates:
        try:
            result = await dispatch_event(event, candidate.apprise_url)
            results.append({
                "source": candidate.source,
                "source_id": candidate.source_id,
                "success": result.success,
                "reason": result.reason,
            })
            if result.success:
                logger.info("Notification dispatched", source=candidate.source, source_id=candidate.source_id)
            else:
                logger.warning("Notification failed", source=candidate.source, source_id=candidate.source_id, reason=result.reason)
        except Exception as exc:
            logger.exception("Notification dispatch error", source=candidate.source, source_id=candidate.source_id, exc=str(exc))
            results.append({
                "source": candidate.source,
                "source_id": candidate.source_id,
                "success": False,
                "reason": str(exc),
            })

    audit(session, EventType.NOTIFICATION_DISPATCHED, watch_id=event.watch_id,
          watch_event_type=event.event_type, results=results)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/workers/test_notify_templates.py tests/workers/test_notify.py -v
```

Expected: all PASS

- [ ] **Step 5: Run full unit suite**

```bash
uv run pytest --no-cov -m "not integration" -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/workers/notify.py tests/workers/test_notify_templates.py
git commit -m "#85 feat: union dispatch — local configs + template refs"
```

---

## Task 5: Auto-Assignment on Watch Create

**Files:**
- Modify: `src/api/routes/watches.py` (find the create watch endpoint)
- Modify: `src/core/models/__init__.py` if needed (verify WatchNcRef, DomainNcRef exported)

The create watch endpoint (`POST /api/v1/watches`) must, after committing the new watch, query global defaults and domain defaults and insert `WatchNcRef` rows. This is non-fatal: assignment failure logs a warning but does not roll back watch creation.

- [ ] **Step 1: Write failing integration test**

```python
# In tests/api/test_watches.py (add to existing file) or tests/api/test_watch_nc_auto_assign.py

import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"

@pytest.mark.integration
async def test_watch_create_assigns_global_default_template(client: AsyncClient, session):
    """A global-default template is auto-assigned to a newly created watch."""
    from src.core.models import NotificationTemplate, WatchNcRef
    from src.core.crypto import encrypt_apprise_url

    # Create a global default template
    tpl = NotificationTemplate(
        title="Global Slack",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        is_global_default=True,
        is_active=True,
    )
    session.add(tpl)
    await session.commit()

    # Create a watch
    resp = await client.post("/api/v1/watches", json={
        "name": "Auto-assign Test",
        "url": "https://example.com",
        "content_type": "html",
    })
    assert resp.status_code == 201
    watch_id = resp.json()["id"]

    # Verify the ref was created
    from sqlalchemy import select
    from ulid import ULID
    result = await session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert len(refs) == 1
    assert str(refs[0].template_id) == str(tpl.id)
```

- [ ] **Step 2: Run to confirm failure**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/api/test_watch_nc_auto_assign.py -v -m integration
```

Expected: FAIL (no refs created)

- [ ] **Step 3: Implement auto-assignment helper**

Add a helper function in `src/api/routes/watches.py` (or a small helper module — keep it in the watches route file to avoid premature abstraction):

```python
async def _assign_default_templates(session: AsyncSession, watch: Watch) -> None:
    """Assign global and domain NC defaults to a newly created watch. Non-fatal."""
    from src.core.models.notification_template import NotificationTemplate, WatchNcRef, DomainNcRef
    try:
        template_ids: set = set()

        # Global defaults
        global_result = await session.execute(
            select(NotificationTemplate.id).where(
                NotificationTemplate.is_global_default.is_(True),
                NotificationTemplate.is_active.is_(True),
            )
        )
        template_ids.update(row[0] for row in global_result)

        # Domain defaults
        if watch.effective_domain:
            domain_result = await session.execute(
                select(DomainNcRef.template_id).where(
                    DomainNcRef.domain_name == watch.effective_domain
                )
            )
            template_ids.update(row[0] for row in domain_result)

        for template_id in template_ids:
            session.add(WatchNcRef(watch_id=watch.id, template_id=template_id))

        if template_ids:
            await session.flush()

    except Exception:
        logger.warning("Failed to assign default NC templates to watch", watch_id=str(watch.id))
```

Call `await _assign_default_templates(session, watch)` immediately after the watch is committed in the create endpoint.

- [ ] **Step 4: Run to confirm pass**

```bash
uv run pytest tests/api/test_watch_nc_auto_assign.py -v -m integration
```

Expected: PASS

- [ ] **Step 5: Run full integration suite**

```bash
uv run pytest -m integration -q
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/watches.py tests/api/test_watch_nc_auto_assign.py
git commit -m "#85 feat: auto-assign global/domain NC template defaults on watch create"
```

---

## Task 6: API Routes for Notification Templates CRUD

**Files:**
- Create: `src/api/schemas/notification_template.py`
- Create: `src/api/routes/notification_templates.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_notification_templates.py`

- [ ] **Step 1: Write failing integration tests**

```python
# tests/api/test_notification_templates.py
import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"

@pytest.mark.integration
async def test_create_template(client: AsyncClient):
    resp = await client.post("/api/v1/notifications/templates", json={
        "title": "Ops Slack",
        "apprise_url": VALID_URL,
        "events": ["change_detected"],
        "is_global_default": False,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Ops Slack"
    assert "id" in data
    assert "apprise_url" not in data  # never exposed


@pytest.mark.integration
async def test_list_templates(client: AsyncClient):
    await client.post("/api/v1/notifications/templates", json={
        "title": "Template A", "apprise_url": VALID_URL, "events": ["change_detected"],
    })
    resp = await client.get("/api/v1/notifications/templates")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.integration
async def test_delete_template_blocked_when_refs_exist(client: AsyncClient):
    """Cannot delete a template that is referenced by a watch."""
    # Create watch
    watch_resp = await client.post("/api/v1/watches", json={
        "name": "W", "url": "https://example.com", "content_type": "html",
    })
    watch_id = watch_resp.json()["id"]

    # Create template and assign to watch
    tpl_resp = await client.post("/api/v1/notifications/templates", json={
        "title": "T", "apprise_url": VALID_URL, "events": ["change_detected"],
    })
    template_id = tpl_resp.json()["id"]
    await client.post(f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}")

    # Attempt delete — expect 409
    del_resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert del_resp.status_code == 409


@pytest.mark.integration
async def test_delete_template_succeeds_when_no_refs(client: AsyncClient):
    tpl_resp = await client.post("/api/v1/notifications/templates", json={
        "title": "Unused", "apprise_url": VALID_URL, "events": ["change_detected"],
    })
    template_id = tpl_resp.json()["id"]
    resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert resp.status_code == 204
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/api/test_notification_templates.py -v -m integration
```

Expected: 404 (routes not mounted)

- [ ] **Step 3: Write schemas**

```python
# src/api/schemas/notification_template.py
"""Pydantic schemas for NotificationTemplate API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.api.schemas.notification_config import validate_apprise_url, validate_event_list


class NotificationTemplateCreate(BaseModel):
    title: str = Field(..., max_length=100)
    apprise_url: str
    events: list[str] = ["change_detected"]
    is_global_default: bool = False

    @field_validator("apprise_url")
    @classmethod
    def check_apprise_url(cls, v: str) -> str:
        return validate_apprise_url(v)

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str]) -> list[str]:
        return validate_event_list(v)


class NotificationTemplateUpdate(BaseModel):
    title: str | None = Field(None, max_length=100)
    apprise_url: str | None = None
    events: list[str] | None = None
    is_global_default: bool | None = None
    is_active: bool | None = None

    @field_validator("apprise_url")
    @classmethod
    def check_apprise_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_apprise_url(v)
        return v

    @field_validator("events")
    @classmethod
    def check_events(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            return validate_event_list(v)
        return v


class NotificationTemplateResponse(BaseModel):
    id: str
    title: str
    channel_hint: str
    events: list[str]
    is_global_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    watch_ref_count: int = 0
    domain_ref_count: int = 0

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Write route file**

```python
# src/api/routes/notification_templates.py
"""CRUD API for shared notification templates."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from src.api.schemas.notification_config import extract_channel_hint
from src.core.crypto import encrypt_apprise_url
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef

router = APIRouter(prefix="/notifications/templates", tags=["notification-templates"])
logger = get_logger(__name__)


async def _get_template_or_404(template_id: str, session: AsyncSession) -> NotificationTemplate:
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)  # type: ignore[arg-type]
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.post("", status_code=201, response_model=NotificationTemplateResponse)
async def create_template(
    data: NotificationTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    tpl = NotificationTemplate(
        title=data.title,
        apprise_url=encrypt_apprise_url(data.apprise_url),
        channel_hint=extract_channel_hint(data.apprise_url),
        events=data.events,
        is_global_default=data.is_global_default,
    )
    session.add(tpl)
    await session.flush()
    audit(session, EventType.NOTIFICATION_TEMPLATE_CREATED, template_id=str(tpl.id))
    await session.commit()
    return NotificationTemplateResponse(**tpl.__dict__, watch_ref_count=0, domain_ref_count=0)


@router.get("", response_model=list[NotificationTemplateResponse])
async def list_templates(
    session: AsyncSession = Depends(get_db_session),
) -> list[NotificationTemplateResponse]:
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    templates = result.scalars().all()
    # Fetch ref counts in one query
    watch_counts_result = await session.execute(
        select(WatchNcRef.template_id, func.count().label("cnt"))
        .group_by(WatchNcRef.template_id)
    )
    watch_counts = {str(row.template_id): row.cnt for row in watch_counts_result}
    domain_counts_result = await session.execute(
        select(DomainNcRef.template_id, func.count().label("cnt"))
        .group_by(DomainNcRef.template_id)
    )
    domain_counts = {str(row.template_id): row.cnt for row in domain_counts_result}
    return [
        NotificationTemplateResponse(
            **tpl.__dict__,
            watch_ref_count=watch_counts.get(str(tpl.id), 0),
            domain_ref_count=domain_counts.get(str(tpl.id), 0),
        )
        for tpl in templates
    ]


@router.get("/{template_id}", response_model=NotificationTemplateResponse)
async def get_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    tpl = await _get_template_or_404(template_id, session)
    watch_count = await session.scalar(
        select(func.count()).where(WatchNcRef.template_id == tpl.id)  # type: ignore[arg-type]
    ) or 0
    domain_count = await session.scalar(
        select(func.count()).where(DomainNcRef.template_id == tpl.id)  # type: ignore[arg-type]
    ) or 0
    return NotificationTemplateResponse(**tpl.__dict__, watch_ref_count=watch_count, domain_ref_count=domain_count)


@router.patch("/{template_id}", response_model=NotificationTemplateResponse)
async def update_template(
    template_id: str,
    data: NotificationTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    tpl = await _get_template_or_404(template_id, session)
    if data.apprise_url is not None:
        tpl.apprise_url = encrypt_apprise_url(data.apprise_url)
        tpl.channel_hint = extract_channel_hint(data.apprise_url)
    if data.events is not None:
        tpl.events = data.events
    if data.is_global_default is not None:
        tpl.is_global_default = data.is_global_default
    if data.is_active is not None:
        tpl.is_active = data.is_active
    if "title" in data.model_fields_set and data.title is not None:
        tpl.title = data.title
    audit(session, EventType.NOTIFICATION_TEMPLATE_UPDATED, template_id=str(tpl.id))
    await session.commit()
    watch_count = await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    domain_count = await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    return NotificationTemplateResponse(**tpl.__dict__, watch_ref_count=watch_count, domain_ref_count=domain_count)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    tpl = await _get_template_or_404(template_id, session)
    watch_count = await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    domain_count = await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    if watch_count or domain_count:
        raise HTTPException(
            status_code=409,
            detail=f"Template is referenced by {watch_count} watch(es) and {domain_count} domain(s). Unassign all references first.",
        )
    audit(session, EventType.NOTIFICATION_TEMPLATE_DELETED, template_id=str(tpl.id))
    await session.delete(tpl)
    await session.commit()


@router.post("/{template_id}/assign/{watch_id}", status_code=201)
async def assign_template_to_watch(
    template_id: str,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    tpl = await _get_template_or_404(template_id, session)
    watch_ulid = ULID.from_str(watch_id)
    # Idempotent: check if ref already exists
    existing = await session.scalar(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch_ulid,
            WatchNcRef.template_id == tpl.id,
        )
    )
    if not existing:
        session.add(WatchNcRef(watch_id=watch_ulid, template_id=tpl.id))
        audit(session, EventType.WATCH_NC_ASSIGNED, watch_id=watch_id, template_id=template_id)
        await session.commit()
    return {"assigned": True}


@router.delete("/{template_id}/assign/{watch_id}", status_code=204)
async def unassign_template_from_watch(
    template_id: str,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    watch_ulid = ULID.from_str(watch_id)
    result = await session.execute(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch_ulid,
            WatchNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    ref = result.scalar_one_or_none()
    if ref:
        await session.delete(ref)
        audit(session, EventType.WATCH_NC_UNASSIGNED, watch_id=watch_id, template_id=template_id)
        await session.commit()


@router.post("/{template_id}/test")
async def test_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    from datetime import datetime, timezone
    from src.core.notifications.events import WatchEvent, WatchEventType
    from src.core.notifications.dispatcher import dispatch_event
    from src.core.crypto import decrypt_apprise_url

    tpl = await _get_template_or_404(template_id, session)
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id="00000000000000000000000000",
        watch_name="[Test]",
        watch_url="https://example.com",
        occurred_at=datetime.now(timezone.utc),
    )
    try:
        url = decrypt_apprise_url(tpl.apprise_url)
        result = await dispatch_event(event, url)
        audit(session, EventType.NOTIFICATION_TEMPLATE_TESTED, template_id=template_id)
        await session.commit()
        return {"success": result.success, "reason": result.reason}
    except Exception as exc:
        return {"success": False, "reason": str(exc)}
```

- [ ] **Step 5: Mount router in main.py**

In `src/api/main.py`, add:
```python
from src.api.routes.notification_templates import router as notification_templates_router
# ...
v1_router.include_router(notification_templates_router)
```

- [ ] **Step 6: Run tests to confirm pass**

```bash
uv run pytest tests/api/test_notification_templates.py -v -m integration
```

Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/api/schemas/notification_template.py src/api/routes/notification_templates.py src/api/main.py tests/api/test_notification_templates.py
git commit -m "#85 feat: notification templates CRUD API with assign/unassign/test endpoints"
```

---

## Task 7: Dashboard /notifications Page

**Files:**
- Modify: `src/dashboard/routes.py` — add `/notifications`, `/notifications/{id}/edit-form`, `/notifications/{id}/edit`, `/notifications/{id}/toggle`, `/notifications/{id}/delete`, `/notifications/{id}/test-result`; add domain NC default routes
- Modify: `src/dashboard/templates/base.html` — add nav link
- Create: `src/dashboard/templates/pages/notifications.html`
- Create: `src/dashboard/templates/partials/notification_template_row.html`
- Create: `src/dashboard/templates/partials/notification_template_add_row.html`
- Create: `src/dashboard/templates/partials/notification_template_edit_form.html`

The /notifications page is the library management interface. It follows the same HTMX patterns as other dashboard pages.

- [ ] **Step 1: Add nav link to base.html**

In both desktop nav and mobile nav sections, add after the Watches link:
```html
<a href="/notifications"
   class="nav-link {% if active_page == 'notifications' %}nav-link-active{% endif %}">
  Notifications
</a>
```

- [ ] **Step 2: Add dashboard routes**

In `src/dashboard/routes.py`, add the following imports (if not already present):
```python
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url
```

Add routes:

```python
@router.get("/notifications")
async def notifications_page(request: Request, session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    apprise_plugins = list_plugins()
    return templates.TemplateResponse(
        request,
        "pages/notifications.html",
        {
            "active_page": "notifications",
            "notification_templates": notification_templates,
            "apprise_plugins": apprise_plugins,
        },
    )


@router.get("/notifications/add-row")
async def notification_template_add_row(request: Request, session: AsyncSession = Depends(get_db_session)):
    apprise_plugins = list_plugins()
    return templates.TemplateResponse(
        request,
        "partials/notification_template_add_row.html",
        {"apprise_plugins": apprise_plugins},
    )


@router.post("/notifications/new")
async def notification_template_create(request: Request, session: AsyncSession = Depends(get_db_session)):
    """Create a new notification template from dashboard form."""
    form = await request.form()
    # Parse apprise_url: prefer plugin_schema+tokens (via assemble_url), fall back to raw apprise_url field
    # Validate events (must be non-empty list of valid WatchEventType values)
    # On validation error: return notification_template_add_row.html with error= context
    # On success: create NotificationTemplate, audit, commit; return refreshed template list via
    #   TemplateResponse("partials/notification_template_list.html", {...}) with
    #   HX-Trigger: "refreshTemplates" header to update the main table
    # Follow the exact same pattern as watch_notification_create in routes.py
    ...


@router.get("/notifications/{template_id}/edit-form")
async def notification_template_edit_form(
    request: Request, template_id: str, session: AsyncSession = Depends(get_db_session)
):
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)  # type: ignore[arg-type]
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404)
    decrypted_url = ""
    decryption_failed = False
    try:
        decrypted_url = decrypt_apprise_url(tpl.apprise_url)
    except Exception:
        decryption_failed = True
    watch_count = await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    domain_count = await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0  # type: ignore[arg-type]
    return templates.TemplateResponse(
        request,
        "partials/notification_template_edit_form.html",
        {
            "tpl": tpl,
            "decrypted_url": decrypted_url,
            "decryption_failed": decryption_failed,
            "watch_count": watch_count,
            "domain_count": domain_count,
        },
    )
```

Implement `POST /notifications/{template_id}/edit`, `POST /notifications/{template_id}/toggle`, `POST /notifications/{template_id}/delete`, `POST /notifications/{template_id}/test-result` following the same HTMX swap patterns as existing watch NC routes.

- [ ] **Step 3: Create page templates**

`pages/notifications.html` — extends `base.html`. Title "Notification Templates". Main content: add-row trigger button, data table with columns (Title, Channel, Events, Global Default, Active, Actions). Uses `#templates-tbody` as swap target with `hx-trigger="refreshTemplates from:body"`. Empty state when no templates.

`partials/notification_template_row.html` — single `<tr>` for a template. Lock/badge for global default. Actions: Edit (GET edit-form, outerHTML swap), Test (POST test-result, OOB flash), Toggle (POST toggle), Delete (POST delete, confirm dialog if ref_count > 0).

`partials/notification_template_add_row.html` — mirrors `notification_add_row.html`. Apprise plugin picker + token form + events checkboxes + optional title + is_global_default checkbox. Submits to `POST /notifications/new`.

`partials/notification_template_edit_form.html` — mirrors `notification_edit_form.html`. Shows ref counts ("Used by N watches, M domains"). Apprise URL field + events checkboxes + title + is_global_default toggle. Submits to `POST /notifications/{template_id}/edit`.

- [ ] **Step 4: Write failing integration test for POST /notifications/new**

```python
# tests/dashboard/test_notifications_page.py
import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"

@pytest.mark.integration
async def test_create_template_via_dashboard_form(client: AsyncClient, session):
    """POST /notifications/new creates a template and returns the table partial."""
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Ops Alert",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
            "is_global_default": "",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    from sqlalchemy import select
    from src.core.models.notification_template import NotificationTemplate
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.title == "Ops Alert")
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.integration
async def test_create_template_via_dashboard_form_invalid_url(client: AsyncClient):
    """Invalid Apprise URL returns add-row partial with error, no DB write."""
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Bad",
            "apprise_url": "not-a-valid-apprise-url",
            "events": ["change_detected"],
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert b"error" in resp.content.lower() or b"invalid" in resp.content.lower()
```

- [ ] **Step 5: Run to confirm failure**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/dashboard/test_notifications_page.py -v -m integration
```

Expected: 404 or 405 (route not implemented)

- [ ] **Step 6: Implement POST /notifications/new**

Follow the exact pattern of `watch_notification_create` in `src/dashboard/routes.py` — parse `plugin_schema`/tokens via `assemble_url` or fall back to raw `apprise_url`, validate events, create `NotificationTemplate`, audit, commit. On error return `notification_template_add_row.html` with `error=` in context. On success return refreshed template list partial with `HX-Trigger: refreshTemplates` response header.

- [ ] **Step 7: Run to confirm pass**

```bash
uv run pytest tests/dashboard/test_notifications_page.py -v -m integration
```

Expected: all PASS

- [ ] **Step 8: Manual smoke test**

Start dev server and visit `https://watcher.exe.xyz:8001/notifications`. Verify:
- Page loads with empty state
- "Add" opens inline form
- Creating a template shows it in the table
- Edit form populates correctly
- Global default toggle persists

- [ ] **Step 9: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/ tests/dashboard/test_notifications_page.py
git commit -m "#85 feat: /notifications dashboard page — template library CRUD UI"
```

---

## Task 8: Watch Detail NC Section Redesign

**Files:**
- Modify: `src/dashboard/routes.py` — add watch NC assign/unassign/copy routes; update `partial_watch_notifications` to include template refs
- Modify: `src/dashboard/templates/partials/watch_notifications.html` — Library/Local visual groups
- Modify: `src/dashboard/templates/partials/notification_add_row.html` — split Add button
- Create: `src/dashboard/templates/partials/watch_nc_assign_row.html` — assign-from-library picker form

- [ ] **Step 1: Update partial_watch_notifications context**

In `src/dashboard/routes.py`, in the `partial_watch_notifications` handler (or wherever the notification table partial is rendered), add a second query for template refs:

```python
# Existing: fetch local watch_notification_configs
# Add: fetch assigned templates
template_refs_result = await session.execute(
    select(NotificationTemplate, WatchNcRef.created_at.label("assigned_at"))
    .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
    .where(WatchNcRef.watch_id == watch_ulid)
    .order_by(NotificationTemplate.title)
)
assigned_templates = template_refs_result.all()

# Also fetch all templates for the assign-from-library picker (not yet assigned)
all_templates_result = await session.execute(
    select(NotificationTemplate)
    .where(NotificationTemplate.is_active.is_(True))
    .order_by(NotificationTemplate.title)
)
all_templates = all_templates_result.scalars().all()
assigned_template_ids = {str(t.id) for t, _ in assigned_templates}
unassigned_templates = [t for t in all_templates if str(t.id) not in assigned_template_ids]
```

Pass `assigned_templates` and `unassigned_templates` in the template context alongside existing `notifications`.

- [ ] **Step 2: Add new dashboard routes**

```python
@router.get("/watches/{watch_id}/notifications/assign-row")
async def watch_nc_assign_row(request, watch_id, session):
    """HTMX: inline form to assign a library template to this watch."""
    # fetch unassigned templates, return watch_nc_assign_row.html partial

@router.post("/watches/{watch_id}/notifications/assign/{template_id}")
async def watch_nc_assign(request, watch_id, template_id, session):
    """Assign library template to watch. Returns refreshed notifications partial."""

@router.post("/watches/{watch_id}/notifications/unassign/{template_id}")
async def watch_nc_unassign(request, watch_id, template_id, session):
    """Unassign library template from watch. Returns refreshed notifications partial."""

@router.post("/watches/{watch_id}/notifications/{config_id}/copy")
async def watch_nc_copy(request, watch_id, config_id, session):
    """Copy a local NC as a new local NC on the same watch. Returns refreshed partial."""

@router.post("/watches/{watch_id}/notifications/copy-template/{template_id}")
async def watch_nc_copy_template(request, watch_id, template_id, session):
    """Copy-to-local: decrypt template URL, create WatchNotificationConfig, unassign ref. Returns refreshed partial."""
```

- [ ] **Step 3: Redesign watch_notifications.html**

Replace the single flat table with two visual groups:

```html
{# Library (inherited) group #}
{% if assigned_templates %}
<section>
  <h3 class="...">Library <span aria-hidden="true">🔒</span></h3>
  <table class="data-table">
    <thead>...</thead>
    <tbody>
      {% for tpl, assigned_at in assigned_templates %}
      <tr class="bg-co-purple-50 dark:bg-co-purple-950/20">
        <td>
          <span aria-label="Shared template" class="...">🔒</span>
          {{ tpl.title }}
        </td>
        <td><span class="chip">{{ tpl.channel_hint }}</span></td>
        <td>...</td> {# events #}
        <td>
          {# Unassign button — POST /watches/{watch_id}/notifications/unassign/{tpl.id} #}
          {# Test button #}
          {# Copy-to-local button — POST /watches/{watch_id}/notifications/copy-template/{tpl.id} #}
          {# "Edit in library" link → /notifications/{tpl.id}/edit-form #}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}

{# Local group #}
<section>
  <h3 class="...">Local</h3>
  <table class="data-table" id="notifications-tbody">
    {# existing local NC rows unchanged #}
  </table>
</section>
```

- [ ] **Step 4: Update notification_add_row.html add button**

Replace the single "Add" trigger with a two-option disclosure:

```html
<div class="flex gap-2">
  <button
    hx-get="/watches/{{ watch.id }}/notifications/add-row"
    hx-target="#notifications-tbody"
    hx-swap="afterbegin"
    class="btn">
    Add Local NC
  </button>
  <button
    hx-get="/watches/{{ watch.id }}/notifications/assign-row"
    hx-target="#notifications-tbody"
    hx-swap="afterbegin"
    class="btn btn-secondary">
    Assign from Library
  </button>
</div>
```

- [ ] **Step 5: Create watch_nc_assign_row.html**

Inline form with a `<select>` populated from `unassigned_templates`. Submits to `POST /watches/{watch_id}/notifications/assign/{template_id}`.

- [ ] **Step 6: Write integration tests for new watch NC routes**

```python
# tests/dashboard/test_watch_nc_dashboard.py
import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"

@pytest.mark.integration
async def test_assign_template_to_watch_via_dashboard(client: AsyncClient, session):
    from src.core.models import NotificationTemplate
    from src.core.crypto import encrypt_apprise_url
    tpl = NotificationTemplate(
        title="T", apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json", events=["change_detected"],
    )
    session.add(tpl)
    watch_resp = await client.post("/api/v1/watches", json={
        "name": "W", "url": "https://example.com", "content_type": "html",
    })
    watch_id = watch_resp.json()["id"]
    await session.commit()

    resp = await client.post(
        f"/watches/{watch_id}/notifications/assign/{tpl.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    # Verify ref exists
    from sqlalchemy import select
    from src.core.models.notification_template import WatchNcRef
    from ulid import ULID
    result = await session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.integration
async def test_copy_template_to_local(client: AsyncClient, session):
    from src.core.models import NotificationTemplate, WatchNotificationConfig
    from src.core.crypto import encrypt_apprise_url
    from sqlalchemy import select
    from ulid import ULID

    tpl = NotificationTemplate(
        title="T", apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json", events=["change_detected"],
    )
    session.add(tpl)
    watch_resp = await client.post("/api/v1/watches", json={
        "name": "W", "url": "https://example.com", "content_type": "html",
    })
    watch_id = watch_resp.json()["id"]
    await session.commit()

    # Assign then copy-to-local
    await client.post(f"/watches/{watch_id}/notifications/assign/{tpl.id}", headers={"HX-Request": "true"})
    resp = await client.post(
        f"/watches/{watch_id}/notifications/copy-template/{tpl.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    # WatchNcRef should be gone, WatchNotificationConfig should exist
    from src.core.models.notification_template import WatchNcRef
    ref = await session.scalar(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    assert ref is None

    local = await session.scalar(
        select(WatchNotificationConfig).where(WatchNotificationConfig.watch_id == ULID.from_str(watch_id))
    )
    assert local is not None
```

- [ ] **Step 7: Run integration tests**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/dashboard/test_watch_nc_dashboard.py -v -m integration
```

Expected: all PASS

- [ ] **Step 8: Manual smoke test**

On a watch detail page, verify:
- Library group shows assigned templates with lock icon
- Unassign removes from library group, refreshes partial
- Copy-to-local creates a local NC and removes library ref
- Assign-from-library picker shows unassigned templates only
- Local NC "Copy" creates a duplicate local NC

- [ ] **Step 9: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/ tests/dashboard/test_watch_nc_dashboard.py
git commit -m "#85 feat: watch detail NC section — Library/Local groups, assign-from-library, copy-to-local"
```

---

## Task 9: Domain Detail NC Defaults Section

**Files:**
- Modify: `src/dashboard/routes.py` — add domain NC default list/add/remove routes
- Modify: `src/dashboard/templates/pages/domain_detail.html` — add NC defaults section
- Create: `src/dashboard/templates/partials/domain_nc_defaults.html` — NC defaults table partial

- [ ] **Step 1: Add dashboard routes**

```python
@router.get("/domains/{domain_name}/nc-defaults")
async def domain_nc_defaults_partial(request, domain_name, session):
    """HTMX: render domain NC defaults table."""
    result = await session.execute(
        select(NotificationTemplate)
        .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
        .where(DomainNcRef.domain_name == domain_name)
        .order_by(NotificationTemplate.title)
    )
    assigned = result.scalars().all()
    all_templates_result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.is_active.is_(True)).order_by(NotificationTemplate.title)
    )
    all_templates = all_templates_result.scalars().all()
    assigned_ids = {str(t.id) for t in assigned}
    unassigned = [t for t in all_templates if str(t.id) not in assigned_ids]
    return templates.TemplateResponse(
        request, "partials/domain_nc_defaults.html",
        {"domain_name": domain_name, "assigned": assigned, "unassigned": unassigned},
    )


@router.post("/domains/{domain_name}/nc-defaults/add/{template_id}")
async def domain_nc_default_add(request, domain_name, template_id, session):
    existing = await session.scalar(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == domain_name,
            DomainNcRef.template_id == template_id,
        )
    )
    if not existing:
        session.add(DomainNcRef(domain_name=domain_name, template_id=template_id))
        audit(session, EventType.DOMAIN_NC_DEFAULT_ADDED, domain_name=domain_name, template_id=template_id)
        await session.commit()
    # Return refreshed partial

@router.post("/domains/{domain_name}/nc-defaults/remove/{template_id}")
async def domain_nc_default_remove(request, domain_name, template_id, session):
    result = await session.execute(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == domain_name,
            DomainNcRef.template_id == template_id,
        )
    )
    ref = result.scalar_one_or_none()
    if ref:
        await session.delete(ref)
        audit(session, EventType.DOMAIN_NC_DEFAULT_REMOVED, domain_name=domain_name, template_id=template_id)
        await session.commit()
    # Return refreshed partial
```

- [ ] **Step 2: Create domain_nc_defaults.html partial**

Table listing assigned templates with a "Remove from defaults" button per row. Below the table: a picker `<select>` of unassigned templates + "Add as domain default" button. Informational note: *"These templates are automatically assigned to new watches created under this domain."*

- [ ] **Step 3: Add NC defaults section to domain_detail.html**

Add after the existing domain fields section:
```html
<section aria-labelledby="domain-nc-defaults-heading">
  <h2 id="domain-nc-defaults-heading">Notification Defaults</h2>
  <div
    hx-get="/domains/{{ domain.name }}/nc-defaults"
    hx-trigger="load"
    hx-target="this"
    hx-swap="innerHTML">
    Loading…
  </div>
</section>
```

- [ ] **Step 4: Write integration tests for domain NC default routes**

```python
# tests/dashboard/test_domain_nc_dashboard.py
import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"

@pytest.mark.integration
async def test_add_and_remove_domain_nc_default(client: AsyncClient, session):
    from src.core.models import NotificationTemplate, Domain
    from src.core.models.notification_template import DomainNcRef
    from src.core.crypto import encrypt_apprise_url
    from sqlalchemy import select

    # Create domain
    domain = Domain(name="example.com")
    session.add(domain)
    tpl = NotificationTemplate(
        title="D", apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json", events=["change_detected"],
    )
    session.add(tpl)
    await session.commit()

    # Add domain default
    resp = await client.post(
        f"/domains/example.com/nc-defaults/add/{tpl.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    ref = await session.scalar(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == "example.com",
            DomainNcRef.template_id == tpl.id,
        )
    )
    assert ref is not None

    # Remove domain default
    resp = await client.post(
        f"/domains/example.com/nc-defaults/remove/{tpl.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    ref = await session.scalar(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == "example.com",
            DomainNcRef.template_id == tpl.id,
        )
    )
    assert ref is None
```

- [ ] **Step 5: Run integration tests**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/dashboard/test_domain_nc_dashboard.py -v -m integration
```

Expected: all PASS

- [ ] **Step 6: Manual smoke test**

On a domain detail page, verify:
- NC defaults section loads
- Adding a template from the picker creates a domain default
- Removing a default removes it from the table
- Creating a new watch under a domain with defaults correctly auto-assigns them

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/ tests/dashboard/test_domain_nc_dashboard.py
git commit -m "#85 feat: domain detail — NC defaults section with add/remove"
```

---

## Task 10: Final Integration Test Pass

- [ ] **Step 1: Run full test suite**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest -q
```

Expected: all pass, no regressions

- [ ] **Step 2: Run linter**

```bash
uv run ruff check .
```

Fix any issues.

- [ ] **Step 3: Final commit if any lint fixes**

```bash
git add -p  # stage only lint fixes
git commit -m "#85 chore: lint fixes"
```

- [ ] **Step 4: Push branch**

```bash
git push -u origin 85-notification-template-library
```
