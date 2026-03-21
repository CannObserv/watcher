# Phase 6: Notifications — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pluggable notification framework that dispatches change alerts to configured channels (webhook, email, Slack), triggered from the check_watch pipeline when changes are detected.

**Architecture:** A `NotificationChannel` protocol with concrete implementations per channel type. A `NotificationConfig` model stores per-watch channel configuration. A `send_notifications` function in the worker dispatches to all configured channels for a watch when a change is detected. Notifications are audited.

**Tech Stack:** Python `typing.Protocol`, `httpx` (webhook), `smtplib`/`aiosmtplib` (email), `httpx` (Slack incoming webhooks)

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  core/
    notifications/
      __init__.py          — create: exports
      base.py              — create: NotificationChannel protocol, ChangeEvent dataclass
      webhook.py           — create: WebhookChannel implementation
      email.py             — create: EmailChannel implementation
      slack.py             — create: SlackChannel implementation
      dispatcher.py        — create: load configs, dispatch to channels
    models/
      notification_config.py — create: NotificationConfig model
      __init__.py            — modify: add NotificationConfig export
  workers/
    tasks.py               — modify: call dispatcher after change detected
  api/
    schemas/
      notification_config.py — create: Pydantic schemas for config CRUD
    routes/
      notification_configs.py — create: CRUD endpoints nested under watches
    main.py                  — modify: include notification config router
alembic/
  versions/                  — new migration for notification_configs table
tests/
  core/notifications/
    __init__.py              — create
    test_webhook.py          — create
    test_email.py            — create
    test_slack.py            — create
    test_dispatcher.py       — create
  core/
    test_models.py           — modify: add NotificationConfig model tests
  api/
    test_notification_configs.py — create: integration tests
```

---

## Task 1: NotificationChannel protocol and ChangeEvent

**Files:**
- Create: `src/core/notifications/__init__.py`
- Create: `src/core/notifications/base.py`
- Create: `tests/core/notifications/__init__.py`
- Create: `tests/core/notifications/test_base.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/notifications/test_base.py`:

```python
"""Tests for notification base types."""

from datetime import UTC, datetime

from src.core.notifications.base import ChangeEvent


class TestChangeEvent:
    def test_create_event(self):
        event = ChangeEvent(
            watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
            watch_name="Test Watch",
            watch_url="https://example.com",
            change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
            detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
            change_metadata={"added": ["Section 2"], "modified": [], "removed": []},
        )
        assert event.watch_name == "Test Watch"
        assert event.change_metadata["added"] == ["Section 2"]

    def test_summary_property(self):
        event = ChangeEvent(
            watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
            watch_name="WA Cannabis Board",
            watch_url="https://example.com/agenda",
            change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
            detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
            change_metadata={
                "added": ["Page 3"],
                "modified": [{"label": "Page 1", "similarity": 0.92}],
                "removed": [],
            },
        )
        summary = event.summary
        assert "WA Cannabis Board" in summary
        assert "1 added" in summary
        assert "1 modified" in summary
```

- [ ] **Step 2: Implement**

Create `src/core/notifications/__init__.py`:

```python
"""Notification framework — pluggable channels for change alerts."""

from src.core.notifications.base import ChangeEvent, NotificationChannel

__all__ = ["ChangeEvent", "NotificationChannel"]
```

Create `src/core/notifications/base.py`:

```python
"""Notification protocol and shared types."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ChangeEvent:
    """Immutable event representing a detected change, passed to channels."""

    watch_id: str
    watch_name: str
    watch_url: str
    change_id: str
    detected_at: datetime
    change_metadata: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Human-readable one-line summary of the change."""
        parts = []
        added = self.change_metadata.get("added", [])
        modified = self.change_metadata.get("modified", [])
        removed = self.change_metadata.get("removed", [])
        if added:
            parts.append(f"{len(added)} added")
        if modified:
            parts.append(f"{len(modified)} modified")
        if removed:
            parts.append(f"{len(removed)} removed")
        detail = ", ".join(parts) if parts else "content changed"
        return f"Change detected: {self.watch_name} — {detail}"


class NotificationChannel(Protocol):
    """Protocol for notification delivery channels."""

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Send a notification. Returns True on success, False on failure."""
        ...
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
uv run pytest tests/core/notifications/test_base.py -v
uv run ruff check .
git add src/core/notifications/ tests/core/notifications/
git commit -m "#2 feat: add NotificationChannel protocol and ChangeEvent dataclass"
```

---

## Task 2: WebhookChannel

**Files:**
- Create: `src/core/notifications/webhook.py`
- Create: `tests/core/notifications/test_webhook.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for webhook notification channel."""

import httpx
import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.webhook import WebhookChannel


@pytest.fixture
def event():
    from datetime import UTC, datetime
    return ChangeEvent(
        watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
        watch_name="Test Watch",
        watch_url="https://example.com",
        change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
        detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
        change_metadata={"added": ["Page 2"], "modified": [], "removed": []},
    )


class TestWebhookChannel:
    async def test_sends_post_with_json_payload(self, event):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        channel = WebhookChannel(client=client)
        result = await channel.send(event, {"url": "https://hooks.example.com/abc"})
        assert result is True
        assert len(requests) == 1
        assert requests[0].method == "POST"

    async def test_includes_event_data_in_payload(self, event):
        import json
        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        channel = WebhookChannel(client=client)
        await channel.send(event, {"url": "https://hooks.example.com/abc"})
        payload = payloads[0]
        assert payload["watch_name"] == "Test Watch"
        assert payload["change_id"] == "01KM7A9TP2B0BQCNZ5PZX4MH8A"

    async def test_returns_false_on_http_error(self, event):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        )
        channel = WebhookChannel(client=client)
        result = await channel.send(event, {"url": "https://hooks.example.com/abc"})
        assert result is False

    async def test_returns_false_on_connection_error(self, event):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        channel = WebhookChannel(client=client)
        result = await channel.send(event, {"url": "https://hooks.example.com/abc"})
        assert result is False
```

- [ ] **Step 2: Implement**

Create `src/core/notifications/webhook.py`:

```python
"""Webhook notification channel — POST JSON to a configured URL."""

from dataclasses import asdict

import httpx

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class WebhookChannel:
    """Delivers notifications via HTTP POST with JSON payload."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """POST event data to the configured webhook URL."""
        url = config.get("url")
        if not url:
            logger.warning("webhook config missing url")
            return False

        payload = asdict(event)
        payload["detected_at"] = event.detected_at.isoformat()
        payload["summary"] = event.summary

        client = self._client or httpx.AsyncClient()
        own_client = self._client is None
        try:
            response = await client.post(
                url,
                json=payload,
                timeout=10.0,
                headers=config.get("headers", {}),
            )
            if response.status_code >= 400:
                logger.warning(
                    "webhook delivery failed",
                    extra={"url": url, "status": response.status_code},
                )
                return False
            return True
        except httpx.HTTPError:
            logger.warning("webhook delivery error", extra={"url": url}, exc_info=True)
            return False
        finally:
            if own_client:
                await client.aclose()
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
uv run pytest tests/core/notifications/test_webhook.py -v
uv run ruff check .
git add src/core/notifications/webhook.py tests/core/notifications/test_webhook.py
git commit -m "#2 feat: add webhook notification channel"
```

---

## Task 3: EmailChannel

**Files:**
- Create: `src/core/notifications/email.py`
- Create: `tests/core/notifications/test_email.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for email notification channel."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.email import EmailChannel


@pytest.fixture
def event():
    return ChangeEvent(
        watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
        watch_name="Test Watch",
        watch_url="https://example.com",
        change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
        detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
        change_metadata={"added": ["Page 2"], "modified": [], "removed": []},
    )


class TestEmailChannel:
    async def test_sends_email_with_correct_subject(self, event):
        channel = EmailChannel()
        config = {
            "to": "alerts@example.com",
            "from": "watcher@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
        }
        with patch("src.core.notifications.email.aiosmtplib") as mock_smtp:
            mock_smtp.send = AsyncMock(return_value=({}, "OK"))
            result = await channel.send(event, config)

        assert result is True
        mock_smtp.send.assert_called_once()
        msg = mock_smtp.send.call_args[0][0]
        assert "Test Watch" in msg["Subject"]

    async def test_returns_false_on_smtp_error(self, event):
        channel = EmailChannel()
        config = {
            "to": "alerts@example.com",
            "from": "watcher@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
        }
        with patch("src.core.notifications.email.aiosmtplib") as mock_smtp:
            mock_smtp.send = AsyncMock(side_effect=OSError("SMTP connection failed"))
            result = await channel.send(event, config)

        assert result is False

    async def test_missing_config_returns_false(self, event):
        channel = EmailChannel()
        result = await channel.send(event, {})
        assert result is False
```

- [ ] **Step 2: Implement**

First add dependency: `uv add aiosmtplib`

Create `src/core/notifications/email.py`:

```python
"""Email notification channel — send change alerts via SMTP."""

from email.message import EmailMessage

import aiosmtplib

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class EmailChannel:
    """Delivers notifications via SMTP email."""

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Send an email notification for the change event."""
        to_addr = config.get("to")
        from_addr = config.get("from")
        smtp_host = config.get("smtp_host")
        smtp_port = config.get("smtp_port", 587)

        if not all([to_addr, from_addr, smtp_host]):
            logger.warning("email config incomplete", extra={"config_keys": list(config.keys())})
            return False

        msg = EmailMessage()
        msg["Subject"] = event.summary
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(
            f"Watch: {event.watch_name}\n"
            f"URL: {event.watch_url}\n"
            f"Detected at: {event.detected_at.isoformat()}\n"
            f"\n{event.summary}\n"
            f"\nChange ID: {event.change_id}"
        )

        try:
            await aiosmtplib.send(
                msg,
                hostname=smtp_host,
                port=smtp_port,
                username=config.get("smtp_username"),
                password=config.get("smtp_password"),
                start_tls=config.get("start_tls", True),
            )
            return True
        except (OSError, aiosmtplib.SMTPException):
            logger.warning("email delivery failed", exc_info=True)
            return False
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
uv run pytest tests/core/notifications/test_email.py -v
uv run ruff check .
git add src/core/notifications/email.py tests/core/notifications/test_email.py pyproject.toml uv.lock
git commit -m "#2 feat: add email notification channel"
```

---

## Task 4: SlackChannel

**Files:**
- Create: `src/core/notifications/slack.py`
- Create: `tests/core/notifications/test_slack.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for Slack notification channel."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.slack import SlackChannel


@pytest.fixture
def event():
    return ChangeEvent(
        watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
        watch_name="Test Watch",
        watch_url="https://example.com",
        change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
        detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
        change_metadata={"added": ["Page 2"], "modified": [], "removed": []},
    )


class TestSlackChannel:
    async def test_sends_to_webhook_url(self, event):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        channel = SlackChannel(client=client)
        result = await channel.send(event, {"webhook_url": "https://hooks.slack.com/abc"})
        assert result is True
        assert len(requests) == 1

    async def test_payload_has_text_and_blocks(self, event):
        payloads = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(200, text="ok")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        channel = SlackChannel(client=client)
        await channel.send(event, {"webhook_url": "https://hooks.slack.com/abc"})
        payload = payloads[0]
        assert "text" in payload
        assert "Test Watch" in payload["text"]

    async def test_returns_false_on_error(self, event):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="error"))
        )
        channel = SlackChannel(client=client)
        result = await channel.send(event, {"webhook_url": "https://hooks.slack.com/abc"})
        assert result is False
```

- [ ] **Step 2: Implement**

Create `src/core/notifications/slack.py`:

```python
"""Slack notification channel — post to incoming webhook."""

import httpx

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class SlackChannel:
    """Delivers notifications via Slack incoming webhooks."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """Post a formatted message to the configured Slack webhook."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            logger.warning("slack config missing webhook_url")
            return False

        payload = {
            "text": event.summary,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{event.summary}*\n"
                            f"URL: <{event.watch_url}>\n"
                            f"Detected: {event.detected_at.isoformat()}\n"
                            f"Change ID: `{event.change_id}`"
                        ),
                    },
                }
            ],
        }

        client = self._client or httpx.AsyncClient()
        own_client = self._client is None
        try:
            response = await client.post(webhook_url, json=payload, timeout=10.0)
            if response.status_code >= 400:
                logger.warning(
                    "slack delivery failed",
                    extra={"status": response.status_code},
                )
                return False
            return True
        except httpx.HTTPError:
            logger.warning("slack delivery error", exc_info=True)
            return False
        finally:
            if own_client:
                await client.aclose()
```

- [ ] **Step 3: Update __init__.py exports**

```python
from src.core.notifications.base import ChangeEvent, NotificationChannel
from src.core.notifications.email import EmailChannel
from src.core.notifications.slack import SlackChannel
from src.core.notifications.webhook import WebhookChannel

__all__ = [
    "ChangeEvent",
    "EmailChannel",
    "NotificationChannel",
    "SlackChannel",
    "WebhookChannel",
]
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
uv run pytest tests/core/notifications/ -v
uv run ruff check .
git add src/core/notifications/ tests/core/notifications/test_slack.py
git commit -m "#2 feat: add Slack notification channel"
```

---

## Task 5: Notification dispatcher

**Files:**
- Create: `src/core/notifications/dispatcher.py`
- Create: `tests/core/notifications/test_dispatcher.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for notification dispatcher."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.dispatcher import dispatch_notifications


@pytest.fixture
def event():
    return ChangeEvent(
        watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
        watch_name="Test Watch",
        watch_url="https://example.com",
        change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
        detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
        change_metadata={"added": ["Page 2"], "modified": [], "removed": []},
    )


class TestDispatchNotifications:
    async def test_dispatches_to_all_configs(self, event):
        configs = [
            {"channel": "webhook", "url": "https://hooks.example.com/a"},
            {"channel": "webhook", "url": "https://hooks.example.com/b"},
        ]
        mock_channel = AsyncMock()
        mock_channel.send.return_value = True
        channels = {"webhook": mock_channel}

        results = await dispatch_notifications(event, configs, channels)
        assert mock_channel.send.call_count == 2
        assert all(r["success"] for r in results)

    async def test_unknown_channel_skipped(self, event):
        configs = [{"channel": "pigeon", "url": "coop"}]
        results = await dispatch_notifications(event, configs, {})
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "unknown" in results[0]["error"]

    async def test_channel_failure_does_not_block_others(self, event):
        configs = [
            {"channel": "webhook", "url": "https://fail.example.com"},
            {"channel": "slack", "webhook_url": "https://hooks.slack.com/ok"},
        ]
        fail_channel = AsyncMock()
        fail_channel.send.return_value = False
        ok_channel = AsyncMock()
        ok_channel.send.return_value = True

        channels = {"webhook": fail_channel, "slack": ok_channel}
        results = await dispatch_notifications(event, configs, channels)
        assert results[0]["success"] is False
        assert results[1]["success"] is True

    async def test_empty_configs_returns_empty(self, event):
        results = await dispatch_notifications(event, [], {})
        assert results == []
```

- [ ] **Step 2: Implement**

Create `src/core/notifications/dispatcher.py`:

```python
"""Notification dispatcher — route events to configured channels."""

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


async def dispatch_notifications(
    event: ChangeEvent,
    configs: list[dict],
    channels: dict,
) -> list[dict]:
    """Dispatch a change event to all configured notification channels.

    Args:
        event: The change event to notify about.
        configs: List of notification config dicts, each with a "channel" key.
        channels: Map of channel name -> channel instance.

    Returns:
        List of result dicts: {"channel", "success", "error"?}
    """
    results = []
    for config in configs:
        channel_name = config.get("channel", "unknown")
        channel = channels.get(channel_name)
        if not channel:
            logger.warning("unknown notification channel", extra={"channel": channel_name})
            results.append({
                "channel": channel_name,
                "success": False,
                "error": f"unknown channel: {channel_name}",
            })
            continue

        try:
            success = await channel.send(event, config)
            results.append({"channel": channel_name, "success": success})
            if success:
                logger.info("notification sent", extra={"channel": channel_name, "watch_id": event.watch_id})
            else:
                logger.warning("notification failed", extra={"channel": channel_name, "watch_id": event.watch_id})
        except Exception:
            logger.exception("notification error", extra={"channel": channel_name})
            results.append({"channel": channel_name, "success": False, "error": "exception"})

    return results
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
uv run pytest tests/core/notifications/test_dispatcher.py -v
uv run ruff check .
git add src/core/notifications/dispatcher.py tests/core/notifications/test_dispatcher.py
git commit -m "#2 feat: add notification dispatcher"
```

---

## Task 6: NotificationConfig model and migration

**Files:**
- Create: `src/core/models/notification_config.py`
- Modify: `src/core/models/__init__.py`
- Modify: `tests/core/test_models.py`
- New: Alembic migration

- [ ] **Step 1: Write failing tests**

Add to `tests/core/test_models.py`:

```python
from src.core.models.notification_config import NotificationConfig


class TestNotificationConfigModel:
    def test_create_webhook_config(self):
        config = NotificationConfig(
            watch_id=ULID(),
            channel="webhook",
            config={"url": "https://hooks.example.com/abc"},
        )
        assert config.channel == "webhook"
        assert config.is_active is True

    def test_create_email_config(self):
        config = NotificationConfig(
            watch_id=ULID(),
            channel="email",
            config={"to": "alerts@example.com", "from": "watcher@example.com"},
        )
        assert config.channel == "email"

    def test_create_slack_config(self):
        config = NotificationConfig(
            watch_id=ULID(),
            channel="slack",
            config={"webhook_url": "https://hooks.slack.com/abc"},
        )
        assert config.channel == "slack"
```

- [ ] **Step 2: Implement model**

Create `src/core/models/notification_config.py`:

```python
"""NotificationConfig model — per-watch notification channel configuration."""

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class NotificationConfig(Base, TimestampMixin):
    """A notification channel configuration for a specific watch."""

    __tablename__ = "notification_configs"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id"))
    channel: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs):
        """Set Python-side defaults."""
        kwargs.setdefault("config", {})
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
```

Update `src/core/models/__init__.py` to export `NotificationConfig`.

- [ ] **Step 3: Generate and apply migration**

```bash
export $(cat env | xargs)
uv run alembic revision --autogenerate -m "add notification_configs table"
uv run alembic upgrade head
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
uv run pytest tests/core/test_models.py -v
uv run ruff check .
git add src/core/models/ alembic/ tests/core/test_models.py
git commit -m "#2 feat: add NotificationConfig model and migration"
```

---

## Task 7: Wire dispatcher into check_watch pipeline

**Files:**
- Modify: `src/workers/tasks.py`

- [ ] **Step 1: Wire notifications after change detection**

In `check_watch` (after `_run_check_pipeline` returns), if a change was detected:
1. Load the watch's active notification configs from DB
2. Build a `ChangeEvent` from the watch and change data
3. Call `dispatch_notifications`
4. Audit log each notification result

```python
# After _run_check_pipeline returns:
if result.get("change_id"):
    # Load notification configs
    nc_stmt = select(NotificationConfig).where(
        NotificationConfig.watch_id == watch.id,
        NotificationConfig.is_active.is_(True),
    )
    nc_result = await session.execute(nc_stmt)
    nc_configs = [
        {"channel": nc.channel, **nc.config}
        for nc in nc_result.scalars().all()
    ]

    if nc_configs:
        from src.core.notifications import ChangeEvent, WebhookChannel, EmailChannel, SlackChannel
        from src.core.notifications.dispatcher import dispatch_notifications

        event = ChangeEvent(
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch.url,
            change_id=result["change_id"],
            detected_at=datetime.now(UTC),
            change_metadata=change_metadata,  # from the pipeline result
        )
        channels = {
            "webhook": WebhookChannel(),
            "email": EmailChannel(),
            "slack": SlackChannel(),
        }
        notif_results = await dispatch_notifications(event, nc_configs, channels)

        session.add(AuditLog(
            event_type="notification.dispatched",
            watch_id=watch.id,
            payload={
                "change_id": result["change_id"],
                "results": notif_results,
            },
        ))
        await session.commit()
```

NOTE: The implementer needs to read the current `check_watch` function carefully. The change_metadata is created inside `_run_check_pipeline` but not currently returned. Either return it in the result dict, or reload the Change record to get the metadata. Simplest: add `"change_metadata": metadata` to the return dict in `_run_check_pipeline` when a change is detected.

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest -v
uv run pytest -m integration -v
```

- [ ] **Step 3: Commit**

```bash
git add src/workers/tasks.py
git commit -m "#2 feat: wire notification dispatcher into check_watch pipeline"
```

---

## Task 8: NotificationConfig API endpoints

**Files:**
- Create: `src/api/schemas/notification_config.py`
- Create: `src/api/routes/notification_configs.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_notification_configs.py`

- [ ] **Step 1: Create schemas**

```python
"""Pydantic schemas for notification config CRUD."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas.types import ULIDStr


class NotificationConfigCreate(BaseModel):
    """Schema for creating a notification config."""
    channel: str
    config: dict = Field(default_factory=dict)


class NotificationConfigResponse(BaseModel):
    """Schema for returning a notification config."""
    model_config = ConfigDict(from_attributes=True)
    id: ULIDStr
    watch_id: ULIDStr
    channel: str
    config: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: Create routes**

Nested under watches: `POST /api/watches/{watch_id}/notifications`, `GET /api/watches/{watch_id}/notifications`, `DELETE /api/watches/{watch_id}/notifications/{config_id}`. Same pattern as temporal profiles routes.

- [ ] **Step 3: Write integration tests**

```python
"""Integration tests for notification config API endpoints."""
import pytest

pytestmark = pytest.mark.integration


class TestCreateNotificationConfig:
    async def test_create_webhook_config(self, client):
        watch_resp = await client.post("/api/watches", json={
            "name": "Notified Watch", "url": "https://example.com", "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]
        response = await client.post(f"/api/watches/{watch_id}/notifications", json={
            "channel": "webhook",
            "config": {"url": "https://hooks.example.com/abc"},
        })
        assert response.status_code == 201
        data = response.json()
        assert data["channel"] == "webhook"
        assert data["is_active"] is True

    async def test_create_config_invalid_watch(self, client):
        response = await client.post(
            "/api/watches/00000000000000000000000000/notifications",
            json={"channel": "webhook", "config": {}},
        )
        assert response.status_code == 404


class TestListNotificationConfigs:
    async def test_list_configs(self, client):
        watch_resp = await client.post("/api/watches", json={
            "name": "Multi Notify", "url": "https://example.com", "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]
        await client.post(f"/api/watches/{watch_id}/notifications", json={
            "channel": "webhook", "config": {"url": "https://a.example.com"},
        })
        await client.post(f"/api/watches/{watch_id}/notifications", json={
            "channel": "slack", "config": {"webhook_url": "https://hooks.slack.com/b"},
        })
        response = await client.get(f"/api/watches/{watch_id}/notifications")
        assert response.status_code == 200
        assert len(response.json()) == 2


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client):
        watch_resp = await client.post("/api/watches", json={
            "name": "Delete Notify", "url": "https://example.com", "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(f"/api/watches/{watch_id}/notifications", json={
            "channel": "webhook", "config": {"url": "https://hooks.example.com"},
        })
        config_id = create_resp.json()["id"]
        response = await client.delete(f"/api/watches/{watch_id}/notifications/{config_id}")
        assert response.status_code == 204
```

- [ ] **Step 4: Include router, run tests, commit**

```bash
uv run pytest tests/api/test_notification_configs.py -v -m integration
uv run ruff check .
git add src/api/ tests/api/test_notification_configs.py
git commit -m "#2 feat: add notification config API (create, list, delete)"
```

---

## Task 9: Documentation

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update project layout**

Add:
```
src/core/notifications/  — Notification channels (webhook, email, Slack) and dispatcher
```

- [ ] **Step 2: Run full suite, lint, commit**

```bash
uv run pytest -v
uv run pytest -m integration -v
uv run ruff check .
git add AGENTS.md
git commit -m "#2 docs: add notifications to project layout"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | NotificationChannel protocol, ChangeEvent | ~2 unit |
| 2 | WebhookChannel | ~4 unit |
| 3 | EmailChannel | ~3 unit |
| 4 | SlackChannel | ~3 unit |
| 5 | Dispatcher | ~4 unit |
| 6 | NotificationConfig model + migration | ~3 unit |
| 7 | Wire into check_watch | regression |
| 8 | NotificationConfig API | ~4 integration |
| 9 | Documentation | — |

Total: ~23 new automated tests
