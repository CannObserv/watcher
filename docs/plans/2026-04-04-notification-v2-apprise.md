# Notification System v2 — Apprise Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three bespoke notification channels (webhook, Slack, email) with Apprise, a MIT-licensed Python library with 100+ channel implementations; generalize the event model to cover full watch lifecycle; add per-config event opt-in; and encrypt stored Apprise URL credentials.

**Architecture:** `NotificationConfig` rows are rewritten to store a single encrypted Apprise URL string plus an array of opted-in event type codes. A new `WatchEvent` dataclass replaces `ChangeEvent` as the universal event envelope. A generalized dispatcher decrypts the URL, hands it to Apprise, and awaits `async_notify()`. `Watch.health_status` tracks the last check outcome so the worker can detect `watch_error` / `watch_recovered` state transitions.

**Tech Stack:** Python ≥ 3.12, `apprise` (PyPI, MIT), `cryptography` (Fernet symmetric encryption), SQLAlchemy ARRAY column for event lists, PostgreSQL, pytest, Alembic.

---

## Design Decisions (reference these throughout)

- **One row = one Apprise URL.** `NotificationConfig.apprise_url` holds a single URL string (e.g. `slack://T/A/T/#ops`), encrypted with Fernet. `channel_hint` holds the URL scheme for display (e.g. `slack`, `mailto`).
- **`events` array defaults to `["change_detected"]`.** Other event types must be explicitly opted into.
- **`WatchEventType` is a Python `StrEnum` whose values mirror the `notification_event_types` DB table.** DB table is authoritative for persistence/config; enum keeps Python code typed and greppable.
- **`APPRISE_SECRET_KEY` env var** holds a Fernet key (base64-encoded 32 bytes). Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Add to `/etc/watcher/.env`.
- **Only `change_detected`, `watch_error`, `watch_recovered` are wired to dispatch in this plan.** `watch_created`, `watch_paused`, `watch_resumed` are defined in enum/DB table for future wiring.
- **Per-target status from Apprise is `True/False/None` only** — sufficient for now. `None` = nothing dispatched (no targets matched or Apprise had no targets).
- **`apprise_url` is never returned in API responses.** Responses include only `channel_hint`.
- **`ServiceRegistry.get_channels()` is removed** — Apprise replaces the channel abstraction entirely.

---

## Files Affected

### New
- `src/core/crypto.py` — Fernet encrypt/decrypt for Apprise URLs
- `src/core/notifications/events.py` — `WatchEventType` enum + `WatchEvent` dataclass
- `tests/core/test_crypto.py`
- `tests/core/notifications/test_events.py`
- `alembic/versions/<rev1>_add_notification_event_types.py` — seed table + NotificationConfig reshape
- `alembic/versions/<rev2>_add_watch_health_status.py` — Watch.health_status column

### Rewritten
- `src/core/notifications/dispatcher.py` — Apprise-based dispatcher (replaces channel-routing logic)
- `src/core/notifications/__init__.py` — updated exports
- `src/core/models/notification_config.py` — new schema (apprise_url, channel_hint, events)
- `src/core/models/watch.py` — add `health_status` + `WatchHealthStatus` enum
- `src/workers/notify.py` — `dispatch_event_notifications()` replaces `dispatch_change_notifications()`
- `src/workers/tasks.py` — health transition detection, new dispatch calls, remove registry from notify
- `src/api/schemas/notification_config.py` — new request/response schemas
- `src/api/routes/notification_configs.py` — new PATCH endpoint, new validation logic
- `tests/core/notifications/test_dispatcher.py` — rewritten for Apprise
- `tests/core/notifications/conftest.py` — rewritten for WatchEvent
- `tests/api/test_notification_configs.py` — rewritten for new API shape
- `tests/workers/test_tasks.py` — update for health transitions

### Deleted
- `src/core/notifications/base.py`
- `src/core/notifications/webhook.py`
- `src/core/notifications/slack.py`
- `src/core/notifications/email.py`
- `tests/core/notifications/test_base.py`
- `tests/core/notifications/test_webhook.py`
- `tests/core/notifications/test_slack.py`
- `tests/core/notifications/test_email.py`

### Modified
- `src/core/registry.py` — remove `channel_map`, `get_channels()`
- `src/core/models/__init__.py` — add `WatchHealthStatus` export if needed

---

## Task 1: Add Dependencies

**Files:**
- Modify: `pyproject.toml` (via uv)

- [ ] **Step 1: Add apprise and cryptography**

```bash
cd /home/exedev/watcher
uv add apprise cryptography
```

- [ ] **Step 2: Generate a Fernet key and add it to the env file**

```bash
uv run python -c "from cryptography.fernet import Fernet; print('APPRISE_SECRET_KEY=' + Fernet.generate_key().decode())"
# Append the output line to /etc/watcher/.env
```

- [ ] **Step 3: Verify imports work**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run python -c "import apprise; from cryptography.fernet import Fernet; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add apprise and cryptography dependencies"
```

---

## Task 2: Fernet Encryption Utility

**Files:**
- Create: `src/core/crypto.py`
- Create: `tests/core/test_crypto.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_crypto.py`:

```python
"""Tests for Fernet URL encryption utility."""

import os

import pytest
from cryptography.fernet import Fernet

from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def test_encrypt_returns_string():
    token = encrypt_apprise_url("slack://T/A/T/#ops")
    assert isinstance(token, str)
    assert token != "slack://T/A/T/#ops"


def test_round_trip():
    url = "mailtos://user:pass@smtp.example.com"
    assert decrypt_apprise_url(encrypt_apprise_url(url)) == url


def test_different_plaintexts_produce_different_tokens():
    a = encrypt_apprise_url("slack://T/A/T/#ops")
    b = encrypt_apprise_url("slack://X/Y/Z/#dev")
    assert a != b


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("APPRISE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="APPRISE_SECRET_KEY"):
        encrypt_apprise_url("slack://T/A/T/#ops")
```

- [ ] **Step 2: Run to confirm failure**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/core/test_crypto.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` (module doesn't exist yet)

- [ ] **Step 3: Implement `src/core/crypto.py`**

```python
"""Fernet symmetric encryption for Apprise URL credentials."""

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    """Return a Fernet instance keyed from APPRISE_SECRET_KEY env var."""
    key = os.environ.get("APPRISE_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "APPRISE_SECRET_KEY environment variable not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_apprise_url(url: str) -> str:
    """Encrypt an Apprise URL string. Returns a Fernet token (str)."""
    return _get_fernet().encrypt(url.encode()).decode()


def decrypt_apprise_url(token: str) -> str:
    """Decrypt a Fernet-encrypted Apprise URL token. Returns the plaintext URL."""
    return _get_fernet().decrypt(token.encode()).decode()
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/core/test_crypto.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/core/crypto.py tests/core/test_crypto.py
git commit -m "feat: add Fernet encryption utility for Apprise URL credentials"
```

---

## Task 3: WatchEventType + WatchEvent

**Files:**
- Create: `src/core/notifications/events.py`
- Create: `tests/core/notifications/test_events.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/notifications/test_events.py`:

```python
"""Tests for WatchEvent and WatchEventType."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType


OCCURRED_AT = datetime(2026, 4, 4, 12, 0, 0, tzinfo=UTC)


def make_event(event_type, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watch_id="01HV0000000000000000000001",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=OCCURRED_AT,
        metadata=metadata or {},
    )


class TestWatchEventType:
    def test_all_expected_types_exist(self):
        codes = {e.value for e in WatchEventType}
        assert "change_detected" in codes
        assert "watch_error" in codes
        assert "watch_recovered" in codes
        assert "watch_created" in codes
        assert "watch_paused" in codes
        assert "watch_resumed" in codes

    def test_is_str_enum(self):
        assert WatchEventType.CHANGE_DETECTED == "change_detected"


class TestWatchEventImmutable:
    def test_frozen(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        with pytest.raises(Exception):
            event.watch_id = "other"


class TestWatchEventTitle:
    def test_change_detected_title(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        assert "Test Watch" in event.title
        assert "Change Detected" in event.title

    def test_watch_error_title(self):
        event = make_event(WatchEventType.WATCH_ERROR)
        assert "Watch Error" in event.title

    def test_watch_recovered_title(self):
        event = make_event(WatchEventType.WATCH_RECOVERED)
        assert "Watch Recovered" in event.title


class TestWatchEventBody:
    def test_change_detected_with_metadata(self):
        event = make_event(
            WatchEventType.CHANGE_DETECTED,
            metadata={"added": ["sec-a", "sec-b"], "modified": ["sec-c"], "removed": []},
        )
        body = event.body
        assert "2 added" in body
        assert "1 modified" in body
        assert "removed" not in body

    def test_change_detected_empty_metadata(self):
        event = make_event(WatchEventType.CHANGE_DETECTED, metadata={})
        assert "details pending" in event.body

    def test_watch_error_includes_status_code(self):
        event = make_event(WatchEventType.WATCH_ERROR, metadata={"status_code": 503})
        assert "503" in event.body

    def test_watch_recovered_body(self):
        event = make_event(WatchEventType.WATCH_RECOVERED)
        assert "responding normally" in event.body


class TestAppriseNotifyType:
    def test_change_detected_is_info(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        assert event.apprise_notify_type == "info"

    def test_watch_error_is_failure(self):
        event = make_event(WatchEventType.WATCH_ERROR)
        assert event.apprise_notify_type == "failure"

    def test_watch_recovered_is_success(self):
        event = make_event(WatchEventType.WATCH_RECOVERED)
        assert event.apprise_notify_type == "success"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/core/notifications/test_events.py -v
```

Expected: `ImportError` (module doesn't exist)

- [ ] **Step 3: Implement `src/core/notifications/events.py`**

```python
"""WatchEventType enum and WatchEvent dataclass — universal notification envelope."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WatchEventType(enum.StrEnum):
    """Notification event type codes. Values mirror the notification_event_types DB table."""

    CHANGE_DETECTED = "change_detected"
    WATCH_ERROR = "watch_error"
    WATCH_RECOVERED = "watch_recovered"
    WATCH_CREATED = "watch_created"
    WATCH_PAUSED = "watch_paused"
    WATCH_RESUMED = "watch_resumed"


_TITLES: dict[WatchEventType, str] = {
    WatchEventType.CHANGE_DETECTED: "Change Detected",
    WatchEventType.WATCH_ERROR: "Watch Error",
    WatchEventType.WATCH_RECOVERED: "Watch Recovered",
    WatchEventType.WATCH_CREATED: "Watch Created",
    WatchEventType.WATCH_PAUSED: "Watch Paused",
    WatchEventType.WATCH_RESUMED: "Watch Resumed",
}

_APPRISE_TYPES: dict[WatchEventType, str] = {
    WatchEventType.CHANGE_DETECTED: "info",
    WatchEventType.WATCH_ERROR: "failure",
    WatchEventType.WATCH_RECOVERED: "success",
    WatchEventType.WATCH_CREATED: "info",
    WatchEventType.WATCH_PAUSED: "warning",
    WatchEventType.WATCH_RESUMED: "info",
}


@dataclass(frozen=True)
class WatchEvent:
    """Immutable value object describing a watch lifecycle event."""

    event_type: WatchEventType
    watch_id: str
    watch_name: str
    watch_url: str
    occurred_at: datetime
    metadata: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        """Short notification title including watch name."""
        return f"{_TITLES[self.event_type]}: {self.watch_name}"

    @property
    def body(self) -> str:
        """Human-readable notification body."""
        if self.event_type == WatchEventType.CHANGE_DETECTED:
            parts: list[str] = []
            for label in ("added", "modified", "removed"):
                items = self.metadata.get(label, [])
                if items:
                    parts.append(f"{len(items)} {label}")
            detail = ", ".join(parts) if parts else "details pending"
            return f"{self.watch_url} — {detail}"
        if self.event_type == WatchEventType.WATCH_ERROR:
            status = self.metadata.get("status_code", "unknown")
            return f"{self.watch_url} returned HTTP {status}"
        if self.event_type == WatchEventType.WATCH_RECOVERED:
            return f"{self.watch_url} is responding normally again"
        if self.event_type == WatchEventType.WATCH_CREATED:
            return f"Now monitoring {self.watch_url}"
        if self.event_type == WatchEventType.WATCH_PAUSED:
            return f"Watch paused: {self.watch_url}"
        if self.event_type == WatchEventType.WATCH_RESUMED:
            return f"Watch resumed: {self.watch_url}"
        return self.watch_url

    @property
    def apprise_notify_type(self) -> str:
        """Apprise NotifyType string for this event type."""
        return _APPRISE_TYPES[self.event_type]
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/core/notifications/test_events.py -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/events.py tests/core/notifications/test_events.py
git commit -m "feat: add WatchEventType enum and WatchEvent dataclass"
```

---

## Task 4: Apprise Dispatcher

**Files:**
- Rewrite: `src/core/notifications/dispatcher.py`
- Rewrite: `tests/core/notifications/test_dispatcher.py`

- [ ] **Step 1: Write failing tests**

Replace `tests/core/notifications/test_dispatcher.py`:

```python
"""Tests for the Apprise-based notification dispatcher."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def make_event(event_type=WatchEventType.CHANGE_DETECTED, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watch_id="01HV0000000000000000000001",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 4, 4, tzinfo=UTC),
        metadata=metadata or {"added": ["sec-a"], "modified": [], "removed": []},
    )


def make_encrypted_url(url: str) -> str:
    from src.core.crypto import encrypt_apprise_url
    return encrypt_apprise_url(url)


class TestDispatchEvent:
    async def test_returns_true_on_apprise_success(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted)

        assert result is True
        instance.async_notify.assert_awaited_once()

    async def test_returns_false_on_apprise_failure(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=False)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted)

        assert result is False

    async def test_returns_false_on_apprise_none(self):
        """None from async_notify means nothing was dispatched."""
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=None)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted)

        assert result is False

    async def test_returns_false_on_invalid_url(self):
        """add() returning False means Apprise rejected the URL."""
        event = make_event()
        encrypted = make_encrypted_url("notaschema://whatever")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = False
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted)

        assert result is False

    async def test_passes_correct_notify_type(self):
        event = make_event(WatchEventType.WATCH_ERROR, metadata={"status_code": 500})
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            await dispatch_event(event, encrypted)

        call_kwargs = instance.async_notify.call_args.kwargs
        assert call_kwargs["notify_type"] == "failure"

    async def test_passes_title_and_body(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            await dispatch_event(event, encrypted)

        call_kwargs = instance.async_notify.call_args.kwargs
        assert "Test Watch" in call_kwargs["title"]
        assert "example.com" in call_kwargs["body"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/core/notifications/test_dispatcher.py -v
```

Expected: failures (old dispatcher has different interface)

- [ ] **Step 3: Rewrite `src/core/notifications/dispatcher.py`**

```python
"""Apprise-based notification dispatcher."""

import apprise

from src.core.crypto import decrypt_apprise_url
from src.core.logging import get_logger
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


async def dispatch_event(event: WatchEvent, apprise_url_encrypted: str) -> bool:
    """Dispatch a WatchEvent to a single Apprise target.

    Decrypts the stored URL, hands it to Apprise, and awaits async_notify.
    Returns True on success, False on failure or if nothing was dispatched.
    """
    url = decrypt_apprise_url(apprise_url_encrypted)
    ap = apprise.Apprise()
    if not ap.add(url):
        logger.warning(
            "invalid apprise url in notification config",
            extra={"watch_id": event.watch_id, "event_type": event.event_type},
        )
        return False
    result = await ap.async_notify(
        body=event.body,
        title=event.title,
        notify_type=event.apprise_notify_type,
    )
    return result is True
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/core/notifications/test_dispatcher.py -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/dispatcher.py tests/core/notifications/test_dispatcher.py
git commit -m "feat: replace channel dispatcher with Apprise-based dispatch_event"
```

---

## Task 5: Database Migrations

**Files:**
- Create: `alembic/versions/<rev1>_add_notification_event_types_and_reshape_configs.py`
- Create: `alembic/versions/<rev2>_add_watch_health_status.py`

- [ ] **Step 1: Generate migration 1 — notification_event_types table + NotificationConfig reshape**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic revision -m "add_notification_event_types_and_reshape_configs"
```

Edit the generated file. Fill in `upgrade()` and `downgrade()`:

```python
"""Add notification_event_types table and reshape notification_configs for Apprise.

Revision ID: <generated>
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '<generated>'
down_revision = '<previous>'   # fill in from filename
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create notification_event_types catalog table
    op.create_table(
        "notification_event_types",
        sa.Column("code", sa.String(50), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    )

    # 2. Seed event types
    op.bulk_insert(
        sa.table(
            "notification_event_types",
            sa.column("code", sa.String),
            sa.column("label", sa.String),
            sa.column("description", sa.String),
            sa.column("is_active", sa.Boolean),
        ),
        [
            {"code": "change_detected", "label": "Change Detected",
             "description": "Content change detected during a watch check", "is_active": True},
            {"code": "watch_error", "label": "Watch Error",
             "description": "Watch check failed (first failure after success or unknown)", "is_active": True},
            {"code": "watch_recovered", "label": "Watch Recovered",
             "description": "Watch check succeeded after one or more consecutive failures", "is_active": True},
            {"code": "watch_created", "label": "Watch Created",
             "description": "A new watch was created", "is_active": True},
            {"code": "watch_paused", "label": "Watch Paused",
             "description": "A watch was paused (deactivated)", "is_active": True},
            {"code": "watch_resumed", "label": "Watch Resumed",
             "description": "A watch was resumed (reactivated)", "is_active": True},
        ],
    )

    # 3. Reshape notification_configs: drop old columns, add new ones
    # Since this is pre-production with no real data, truncate first for safety
    op.execute("TRUNCATE TABLE notification_configs")
    op.drop_column("notification_configs", "channel")
    op.drop_column("notification_configs", "config")
    op.add_column(
        "notification_configs",
        sa.Column("apprise_url", sa.Text, nullable=False),
    )
    op.add_column(
        "notification_configs",
        sa.Column("channel_hint", sa.String(50), nullable=False),
    )
    op.add_column(
        "notification_configs",
        sa.Column(
            "events",
            postgresql.ARRAY(sa.String(50)),
            nullable=False,
            server_default="'{change_detected}'",
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_configs", "events")
    op.drop_column("notification_configs", "channel_hint")
    op.drop_column("notification_configs", "apprise_url")
    op.add_column("notification_configs", sa.Column("config", postgresql.JSONB, server_default="{}"))
    op.add_column("notification_configs", sa.Column("channel", sa.String(20), nullable=False, server_default="webhook"))
    op.drop_table("notification_event_types")
```

- [ ] **Step 2: Generate migration 2 — Watch.health_status**

```bash
uv run alembic revision -m "add_watch_health_status"
```

Edit the generated file:

```python
"""Add health_status column to watches table.

Revision ID: <generated>
"""

from alembic import op
import sqlalchemy as sa

revision = '<generated>'
down_revision = '<rev1>'   # point to migration 1
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watches",
        sa.Column(
            "health_status",
            sa.String(10),
            nullable=False,
            server_default="unknown",
        ),
    )


def downgrade() -> None:
    op.drop_column("watches", "health_status")
```

- [ ] **Step 3: Apply migrations**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head
```

Expected: both migrations apply without error.

- [ ] **Step 4: Verify schema**

```bash
uv run python -c "
import asyncio
from src.core.database import get_session_factory
from sqlalchemy import text

async def check():
    async with get_session_factory()() as s:
        r = await s.execute(text('SELECT code FROM notification_event_types ORDER BY code'))
        print([row[0] for row in r])

asyncio.run(check())
"
```

Expected: list of 6 event type codes.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/
git commit -m "feat: add notification_event_types table and reshape notification_configs for Apprise"
```

---

## Task 6: Update NotificationConfig Model

**Files:**
- Rewrite: `src/core/models/notification_config.py`

- [ ] **Step 1: Rewrite the model**

Replace `src/core/models/notification_config.py`:

```python
"""NotificationConfig model — per-watch Apprise notification target."""

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class NotificationConfig(Base, TimestampMixin):
    """A single Apprise notification target for a specific watch.

    apprise_url stores the Fernet-encrypted Apprise URL string (e.g. slack://T/A/T/#ops).
    channel_hint stores the URL scheme for display purposes (e.g. "slack", "mailto").
    events is the list of WatchEventType codes this config opts into.
    """

    __tablename__ = "notification_configs"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id", ondelete="CASCADE"))
    apprise_url: Mapped[str] = mapped_column(Text, nullable=False)
    channel_hint: Mapped[str] = mapped_column(String(50), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        default=list,
        server_default="'{change_detected}'",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs):
        """Set Python-side defaults."""
        kwargs.setdefault("events", ["change_detected"])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
```

- [ ] **Step 2: Verify model imports cleanly**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run python -c "from src.core.models.notification_config import NotificationConfig; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Run integration tests to check no regression**

```bash
uv run pytest -m integration -x --ignore=tests/api/test_notification_configs.py -q 2>&1 | tail -20
```

Expected: passes (notification_configs tests are excluded as they'll be rewritten)

- [ ] **Step 4: Commit**

```bash
git add src/core/models/notification_config.py
git commit -m "feat: update NotificationConfig model for Apprise URL + events array"
```

---

## Task 7: Add Watch.health_status

**Files:**
- Modify: `src/core/models/watch.py`

- [ ] **Step 1: Add `WatchHealthStatus` enum and `health_status` column**

In `src/core/models/watch.py`, add after the existing `ContentType` enum:

```python
class WatchHealthStatus(enum.StrEnum):
    """Last known health state of a watch, updated after each check."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"
```

Add `health_status` as a mapped column in the `Watch` class body (after `effective_domain`):

```python
health_status: Mapped[WatchHealthStatus] = mapped_column(
    String(10),
    default=WatchHealthStatus.UNKNOWN,
    server_default="unknown",
)
```

Add to `__init__` defaults:

```python
kwargs.setdefault("health_status", WatchHealthStatus.UNKNOWN)
```

Add a `@validates` method for `health_status` (follow the same pattern as `content_type`):

```python
@validates("health_status")
def validate_health_status(self, _key: str, value: str | WatchHealthStatus) -> WatchHealthStatus:
    """Coerce string values to WatchHealthStatus enum."""
    if isinstance(value, WatchHealthStatus):
        return value
    try:
        return WatchHealthStatus(value)
    except ValueError as exc:
        raise ValueError(f"Invalid health_status: {value!r}") from exc
```

- [ ] **Step 2: Verify import**

```bash
uv run python -c "from src.core.models.watch import Watch, WatchHealthStatus; print('ok')"
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest -m integration -x -q 2>&1 | tail -20
```

Expected: passes (health_status column was added in migration)

- [ ] **Step 4: Commit**

```bash
git add src/core/models/watch.py
git commit -m "feat: add WatchHealthStatus enum and health_status column to Watch"
```

---

## Task 8: Update API Schema and Route

**Files:**
- Rewrite: `src/api/schemas/notification_config.py`
- Rewrite: `src/api/routes/notification_configs.py`
- Rewrite: `tests/api/test_notification_configs.py`

- [ ] **Step 1: Write failing integration tests**

Replace `tests/api/test_notification_configs.py`:

```python
"""Integration tests for notification config API endpoints (Apprise v2)."""

import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.integration

# A real Apprise URL that parses correctly (json:// is always available)
VALID_URL = "json://hooks.example.com/notify"
INVALID_URL = "notaschema://whatever"


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


async def _make_watch(client):
    resp = await client.post(
        "/api/v1/watches",
        json={"name": "Test Watch", "url": "https://example.com", "content_type": "html"},
    )
    return resp.json()["id"]


class TestCreateNotificationConfig:
    async def test_create_with_valid_url(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_hint"] == "json"
        assert data["events"] == ["change_detected"]
        assert data["is_active"] is True
        # apprise_url must NOT be in response
        assert "apprise_url" not in data

    async def test_default_events_is_change_detected(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        assert resp.status_code == 201
        assert resp.json()["events"] == ["change_detected"]

    async def test_invalid_apprise_url_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": INVALID_URL},
        )
        assert resp.status_code == 422

    async def test_invalid_event_type_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["nonexistent_event"]},
        )
        assert resp.status_code == 422

    async def test_invalid_watch_returns_404(self, client):
        resp = await client.post(
            "/api/v1/watches/00000000000000000000000000/notifications",
            json={"apprise_url": VALID_URL},
        )
        assert resp.status_code == 404

    async def test_multiple_events(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected", "watch_error"]},
        )
        assert resp.status_code == 201
        assert set(resp.json()["events"]) == {"change_detected", "watch_error"}


class TestListNotificationConfigs:
    async def test_list_returns_all_configs(self, client):
        watch_id = await _make_watch(client)
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected"]},
        )
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": "json://second.example.com/notify", "events": ["watch_error"]},
        )
        resp = await client.get(f"/api/v1/watches/{watch_id}/notifications")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_list_excludes_other_watch_configs(self, client):
        watch_a = await _make_watch(client)
        watch_b = await _make_watch(client)
        await client.post(
            f"/api/v1/watches/{watch_a}/notifications",
            json={"apprise_url": VALID_URL},
        )
        resp = await client.get(f"/api/v1/watches/{watch_b}/notifications")
        assert resp.json() == []


class TestPatchNotificationConfig:
    async def test_toggle_is_active(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_update_events(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"events": ["watch_error", "watch_recovered"]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["events"]) == {"watch_error", "watch_recovered"}

    async def test_patch_invalid_event_type_returns_422(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"events": ["bad_event"]},
        )
        assert resp.status_code == 422

    async def test_patch_wrong_watch_returns_404(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        other_watch_id = await _make_watch(client)
        resp = await client.patch(
            f"/api/v1/watches/{other_watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 404


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/watches/{watch_id}/notifications/{config_id}")
        assert resp.status_code == 204

    async def test_delete_wrong_watch_returns_404(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        other = await _make_watch(client)
        resp = await client.delete(f"/api/v1/watches/{other}/notifications/{config_id}")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run to confirm failures**

```bash
uv run pytest tests/api/test_notification_configs.py -v 2>&1 | tail -30
```

Expected: failures (old schema, no PATCH endpoint)

- [ ] **Step 3: Rewrite `src/api/schemas/notification_config.py`**

```python
"""Pydantic schemas for notification config CRUD (Apprise v2)."""

from datetime import datetime
from typing import Annotated

import apprise
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas.types import ULIDStr
from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


def _validate_apprise_url(url: str) -> str:
    """Reject URLs that Apprise cannot parse."""
    ap = apprise.Apprise()
    if not ap.add(url):
        raise ValueError(
            f"Invalid Apprise URL: {url!r}. "
            "See https://github.com/caronc/apprise/wiki for valid URL formats."
        )
    return url


def _extract_channel_hint(url: str) -> str:
    """Return the URL scheme portion (e.g. 'slack' from 'slack://...')."""
    return url.split("://")[0].lower() if "://" in url else url.lower()


class NotificationConfigCreate(BaseModel):
    """Request body for creating a notification config."""

    apprise_url: Annotated[str, Field(min_length=1)]
    events: list[str] = Field(default_factory=lambda: ["change_detected"])

    @field_validator("apprise_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_apprise_url(v)

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        invalid = [e for e in v if e not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s): {invalid}. "
                f"Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v


class NotificationConfigUpdate(BaseModel):
    """Request body for PATCH — all fields optional."""

    is_active: bool | None = None
    events: list[str] | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = [e for e in v if e not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s): {invalid}. "
                f"Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v


class NotificationConfigResponse(BaseModel):
    """Response schema — never exposes apprise_url."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    watch_id: ULIDStr
    channel_hint: str
    events: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Rewrite `src/api/routes/notification_configs.py`**

```python
"""Notification config CRUD API endpoints (Apprise v2)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigResponse,
    NotificationConfigUpdate,
    _extract_channel_hint,
)
from src.core.crypto import encrypt_apprise_url
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import NotificationConfig

router = APIRouter(prefix="/watches/{watch_id}/notifications", tags=["notification-configs"])


@router.post("", status_code=201, response_model=NotificationConfigResponse)
async def create_notification_config(
    watch_id: str,
    data: NotificationConfigCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification config for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    config = NotificationConfig(
        watch_id=watch.id,
        apprise_url=encrypt_apprise_url(data.apprise_url),
        channel_hint=_extract_channel_hint(data.apprise_url),
        events=data.events,
    )
    session.add(config)
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_CREATED,
        watch_id=watch.id,
        config_id=str(config.id),
        channel_hint=config.channel_hint,
    )
    await session.commit()
    await session.refresh(config)
    return config


@router.get("", response_model=list[NotificationConfigResponse])
async def list_notification_configs(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List notification configs for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    stmt = (
        select(NotificationConfig)
        .where(NotificationConfig.watch_id == watch.id)
        .order_by(NotificationConfig.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.patch("/{config_id}", response_model=NotificationConfigResponse)
async def update_notification_config(
    watch_id: str,
    config_id: str,
    data: NotificationConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update is_active or events on a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    if data.is_active is not None:
        nc.is_active = data.is_active
    if data.events is not None:
        nc.events = data.events
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_UPDATED,
        watch_id=watch.id,
        config_id=str(nc.id),
    )
    await session.commit()
    await session.refresh(nc)
    return nc


@router.delete("/{config_id}", status_code=204)
async def delete_notification_config(
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_DELETED,
        watch_id=watch.id,
        config_id=str(nc.id),
    )
    await session.delete(nc)
    await session.commit()
```

- [ ] **Step 5: Add `NOTIFICATION_CONFIG_UPDATED` to `EventType`**

In `src/core/models/audit_log.py`, add to the `EventType` class:

```python
NOTIFICATION_CONFIG_UPDATED = "notification_config.updated"
```

- [ ] **Step 6: Run the integration tests**

```bash
uv run pytest tests/api/test_notification_configs.py -v
```

Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add src/api/schemas/notification_config.py src/api/routes/notification_configs.py \
        src/core/models/audit_log.py tests/api/test_notification_configs.py
git commit -m "feat: rewrite notification config API for Apprise URL + events, add PATCH endpoint"
```

---

## Task 9: Rewrite workers/notify.py

**Files:**
- Rewrite: `src/workers/notify.py`

- [ ] **Step 1: Write the test**

Create `tests/workers/test_notify.py` (or replace if it exists):

```python
"""Tests for dispatch_event_notifications."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.workers.notify import dispatch_event_notifications


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def make_event(event_type=WatchEventType.CHANGE_DETECTED, watch_id=None):
    return WatchEvent(
        event_type=event_type,
        watch_id=watch_id or str(ULID()),
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 4, 4, tzinfo=UTC),
        metadata={"added": ["s1"], "modified": [], "removed": []},
    )


class TestDispatchEventNotifications:
    async def test_no_matching_configs_is_noop(self):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=mock_result)

        event = make_event()
        await dispatch_event_notifications(session, event)

        session.add.assert_not_called()

    async def test_dispatches_to_matching_config(self):
        from src.core.crypto import encrypt_apprise_url
        from src.core.models.notification_config import NotificationConfig

        watch_ulid = ULID()
        config = MagicMock(spec=NotificationConfig)
        config.id = ULID()
        config.watch_id = watch_ulid
        config.apprise_url = encrypt_apprise_url("json://localhost/notify")
        config.events = ["change_detected"]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [config]
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=mock_result)

        event = make_event(watch_id=str(watch_ulid))

        with patch("src.workers.notify.dispatch_event", new_callable=AsyncMock, return_value=True):
            await dispatch_event_notifications(session, event)

        session.add.assert_called_once()  # audit log entry added

    async def test_failure_does_not_raise(self):
        from src.core.crypto import encrypt_apprise_url
        from src.core.models.notification_config import NotificationConfig

        watch_ulid = ULID()
        config = MagicMock(spec=NotificationConfig)
        config.id = ULID()
        config.watch_id = watch_ulid
        config.apprise_url = encrypt_apprise_url("json://localhost/notify")
        config.events = ["change_detected"]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [config]
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=mock_result)

        event = make_event(watch_id=str(watch_ulid))

        with patch(
            "src.workers.notify.dispatch_event",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            # Should not raise
            await dispatch_event_notifications(session, event)
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/workers/test_notify.py -v
```

- [ ] **Step 3: Rewrite `src/workers/notify.py`**

```python
"""Notification dispatch for watch lifecycle events."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import NotificationConfig
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in NotificationConfig rows.

    Queries configs where watch_id matches, is_active is True, and the event
    type code is in the events array. Dispatches concurrently where possible.
    Failures are logged but never raise. Writes a single audit log entry
    with per-config results. Does not commit; caller is responsible.
    """
    stmt = select(NotificationConfig).where(
        NotificationConfig.watch_id == ULID.from_str(event.watch_id),
        NotificationConfig.is_active.is_(True),
        NotificationConfig.events.contains([event.event_type.value]),
    )
    result = await session.execute(stmt)
    configs = result.scalars().all()
    if not configs:
        return

    results = []
    for config in configs:
        try:
            success = await dispatch_event(event, config.apprise_url)
            results.append({"config_id": str(config.id), "success": success})
            extra = {
                "config_id": str(config.id),
                "watch_id": event.watch_id,
                "event_type": event.event_type,
            }
            if success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception("notification error", extra={"config_id": str(config.id)})
            results.append({"config_id": str(config.id), "success": False, "error": "exception"})

    audit(
        session,
        EventType.NOTIFICATION_DISPATCHED,
        watch_id=event.watch_id,
        event_type=event.event_type,
        results=results,
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/workers/test_notify.py -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/workers/notify.py tests/workers/test_notify.py
git commit -m "feat: generalize notify dispatcher to dispatch_event_notifications for all WatchEvent types"
```

---

## Task 10: Update workers/tasks.py — Health Transitions

**Files:**
- Modify: `src/workers/tasks.py`
- Modify: `tests/workers/test_tasks.py`

This task wires up `watch_error` and `watch_recovered` state-transition detection, updates `Watch.health_status`, and replaces the old `dispatch_change_notifications` call with the new `dispatch_event_notifications`.

- [ ] **Step 1: Add health transition tests to `tests/workers/test_tasks.py`**

Read the existing test file first, then add these test cases to the existing `TestCheckWatch` class (or create it if it doesn't exist in a form that supports these tests):

```python
# Tests to add to tests/workers/test_tasks.py
# Import additions needed at the top of the file:
# from src.core.models.watch import WatchHealthStatus
# from src.core.notifications.events import WatchEventType

class TestCheckWatchHealthTransitions:
    """Test watch_error and watch_recovered state transition events."""

    async def test_fetch_failure_sets_health_error(self, db_session, tmp_path, monkeypatch):
        """First fetch failure transitions health_status to ERROR."""
        watch = Watch(
            name="Health Test",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.OK,
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = False
        mock_fetch_result.status_code = 503
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)

        with _mock_session_factory(db_session):
            reg = ServiceRegistry(fetcher=mock_fetcher)
            await check_watch(str(watch.id), registry=reg)

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.ERROR
        assert any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_no_error_event_on_repeated_failure(self, db_session, tmp_path, monkeypatch):
        """Repeated failures after first do not re-emit watch_error."""
        watch = Watch(
            name="Already Error",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,  # already in error state
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = False
        mock_fetch_result.status_code = 503
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)

        with _mock_session_factory(db_session):
            reg = ServiceRegistry(fetcher=mock_fetcher)
            await check_watch(str(watch.id), registry=reg)

        assert not any(e.event_type == WatchEventType.WATCH_ERROR for e in dispatched_events)

    async def test_recovery_emits_watch_recovered(self, db_session, tmp_path, monkeypatch):
        """Successful fetch after ERROR state emits watch_recovered."""
        watch = Watch(
            name="Recovering",
            url="https://example.com",
            content_type=ContentType.HTML,
            health_status=WatchHealthStatus.ERROR,
        )
        db_session.add(watch)
        await db_session.commit()
        await db_session.refresh(watch)

        content = b"<html><body>hello</body></html>"
        mock_fetch_result = MagicMock()
        mock_fetch_result.is_success = True
        mock_fetch_result.status_code = 200
        mock_fetch_result.content = content
        mock_fetch_result.fetcher_used = "http"
        mock_fetch_result.duration_ms = 100
        mock_fetcher = AsyncMock()
        mock_fetcher.fetch = AsyncMock(return_value=mock_fetch_result)

        dispatched_events = []

        async def fake_dispatch(session, event):
            dispatched_events.append(event)

        monkeypatch.setattr("src.workers.tasks.dispatch_event_notifications", fake_dispatch)

        storage = MagicMock()
        storage.snapshot_path = MagicMock(return_value=str(tmp_path / "snap.html"))
        storage.save = MagicMock()
        storage.exists = MagicMock(return_value=False)
        monkeypatch.setattr("src.workers.tasks.LocalStorage", lambda **kw: storage)

        with _mock_session_factory(db_session):
            reg = ServiceRegistry(fetcher=mock_fetcher)
            await check_watch(str(watch.id), registry=reg)

        await db_session.refresh(watch)
        assert watch.health_status == WatchHealthStatus.OK
        assert any(e.event_type == WatchEventType.WATCH_RECOVERED for e in dispatched_events)
```

- [ ] **Step 2: Run new tests to confirm failure**

```bash
uv run pytest tests/workers/test_tasks.py::TestCheckWatchHealthTransitions -v
```

Expected: failures (health_status not updated, wrong dispatch call)

- [ ] **Step 3: Update `src/workers/tasks.py`**

At the top of the file, update imports:

```python
# Replace:
from src.workers.notify import dispatch_change_notifications

# With:
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.models.watch import WatchHealthStatus
from src.workers.notify import dispatch_event_notifications
```

In `check_watch`, find the fetch failure block and update it:

```python
# Replace the existing fetch failure block:
if not fetch_result.is_success:
    logger.warning(
        "fetch failed",
        extra={"watch_id": watch_id, "status": fetch_result.status_code},
    )
    audit(
        session,
        EventType.CHECK_FETCH_FAILED,
        watch_id=watch.id,
        status_code=fetch_result.status_code,
    )
    # Detect watch_error state transition (only fire on first failure)
    previous_health = watch.health_status
    watch.health_status = WatchHealthStatus.ERROR
    await session.commit()
    if previous_health != WatchHealthStatus.ERROR:
        event = WatchEvent(
            event_type=WatchEventType.WATCH_ERROR,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch.url,
            occurred_at=datetime.now(UTC),
            metadata={"status_code": fetch_result.status_code},
        )
        await dispatch_event_notifications(session=session, event=event)
        await session.commit()
    return {"error": f"HTTP {fetch_result.status_code}"}
```

After the pipeline commit in the success path, add health status update and recovery detection. Find the block after `await session.commit()` (after pipeline) and add:

```python
# Update health status + detect recovery
previous_health = watch.health_status
watch.health_status = WatchHealthStatus.OK
watch.last_checked_at = datetime.now(UTC)
await session.commit()

# Decay backoff if needed
_limiter = get_rate_limiter()
_state = _limiter._domains.get(rate_limit_domain)
if _state and _state.current_interval > _state.min_interval:
    await _maybe_decay_backoff(rate_limit_domain, _limiter, session)
    await session.commit()

# Dispatch notifications in a separate transaction scope
if previous_health == WatchHealthStatus.ERROR:
    recovery_event = WatchEvent(
        event_type=WatchEventType.WATCH_RECOVERED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        metadata={},
    )
    await dispatch_event_notifications(session=session, event=recovery_event)
    await session.commit()

if result.get("change_id"):
    change_event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        metadata=result.get("change_metadata", {}),
    )
    await dispatch_event_notifications(session=session, event=change_event)
    await session.commit()
```

Remove the old `watch.last_checked_at = datetime.now(UTC)` line (it's now part of the health update block above).

- [ ] **Step 4: Run all worker tests**

```bash
uv run pytest tests/workers/ -v 2>&1 | tail -40
```

Expected: all pass (including pre-existing tests)

- [ ] **Step 5: Commit**

```bash
git add src/workers/tasks.py tests/workers/test_tasks.py
git commit -m "feat: add health_status tracking and watch_error/watch_recovered transition events to check_watch"
```

---

## Task 11: Cleanup — Delete Old Files and Update Exports

**Files:**
- Delete: `src/core/notifications/base.py`
- Delete: `src/core/notifications/webhook.py`
- Delete: `src/core/notifications/slack.py`
- Delete: `src/core/notifications/email.py`
- Delete: `tests/core/notifications/test_base.py`
- Delete: `tests/core/notifications/test_webhook.py`
- Delete: `tests/core/notifications/test_slack.py`
- Delete: `tests/core/notifications/test_email.py`
- Rewrite: `src/core/notifications/__init__.py`
- Rewrite: `tests/core/notifications/conftest.py`
- Modify: `src/core/registry.py`

- [ ] **Step 1: Delete old channel and test files**

```bash
git rm src/core/notifications/base.py \
       src/core/notifications/webhook.py \
       src/core/notifications/slack.py \
       src/core/notifications/email.py \
       tests/core/notifications/test_base.py \
       tests/core/notifications/test_webhook.py \
       tests/core/notifications/test_slack.py \
       tests/core/notifications/test_email.py
```

- [ ] **Step 2: Rewrite `src/core/notifications/__init__.py`**

```python
"""Notification subsystem — Apprise-based dispatch for watch lifecycle events."""

from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType

__all__ = [
    "WatchEvent",
    "WatchEventType",
    "dispatch_event",
]
```

- [ ] **Step 3: Rewrite `tests/core/notifications/conftest.py`**

```python
"""Shared fixtures for notification tests."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType


@pytest.fixture
def make_event():
    """Factory fixture: build a WatchEvent with sensible defaults."""

    def _make(event_type=WatchEventType.CHANGE_DETECTED, **overrides):
        defaults = {
            "event_type": event_type,
            "watch_id": "01HV0000000000000000000001",
            "watch_name": "Test Watch",
            "watch_url": "https://example.com",
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "metadata": {
                "added": ["Page 2", "Page 3"],
                "modified": ["Page 1"],
                "removed": [],
            },
        }
        defaults.update(overrides)
        return WatchEvent(**defaults)

    return _make
```

- [ ] **Step 4: Remove channel_map from `src/core/registry.py`**

Remove these lines from `registry.py`:
- The `_DEFAULT_CHANNEL_MAP` dict
- The `channel_map` parameter from `ServiceRegistry.__init__`
- The `self._channel_map` attribute
- The `get_channels()` method
- All imports of channel classes (`EmailChannel`, `SlackChannel`, `WebhookChannel`, `NotificationChannel`)

The resulting `ServiceRegistry` should only manage fetcher and extractor.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest -x -q 2>&1 | tail -30
```

Fix any import errors that surface. Expected: all tests pass.

- [ ] **Step 6: Run linter**

```bash
uv run ruff check .
```

Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: remove custom notification channels, clean up registry and exports"
```

---

## Task 12: Final Integration Smoke Test

- [ ] **Step 1: Run the full test suite**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest -v 2>&1 | tail -50
```

Expected: all tests pass, including integration tests (requires `TEST_DATABASE_URL`).

- [ ] **Step 2: Apply migrations to production DB and restart**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic upgrade head
sudo systemctl restart watcher
sudo journalctl -u watcher -f --lines=30
```

Expected: service starts cleanly, no import errors in logs.

- [ ] **Step 3: Verify the API responds**

```bash
curl -s https://watcher.exe.xyz:8000/health | python -m json.tool
curl -s https://watcher.exe.xyz:8000/ready | python -m json.tool
```

Expected: both return `{"status": "ok"}` (or similar).

- [ ] **Step 4: Commit**

No new changes expected. If any fixes were needed, commit them:

```bash
git add -A
git commit -m "fix: post-integration smoke test corrections"
```

---

## Reference: Key Design Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| One row = one Apprise URL | Yes | Simple, maps to existing model |
| `events` default | `["change_detected"]` | Opt-in for everything else |
| Credential storage | Fernet encrypt in DB | No new infra; key in env var |
| `apprise_url` in responses | Never returned | Security; `channel_hint` sufficient |
| Per-target status | `True/False/None` | Sufficient for now; Apprise limitation |
| Dispatch model | In-process library | Simplest; sidecar upgrade path preserved |
| Watch lifecycle events | Enum + DB table defined; `change_detected`/`watch_error`/`watch_recovered` wired | Others (`watch_created` etc.) deferred |
| Health transition detection | `previous_health != ERROR` for errors; `previous_health == ERROR` for recovery | State transition, not every event |
