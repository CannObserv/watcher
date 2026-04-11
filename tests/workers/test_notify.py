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
    def _empty_result(self):
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    def _result_with(self, *items):
        r = MagicMock()
        r.scalars.return_value.all.return_value = list(items)
        return r

    async def test_no_matching_configs_is_noop(self):
        empty = self._empty_result()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[empty, empty])

        event = make_event()
        await dispatch_event_notifications(session, event)

        session.add.assert_not_called()

    async def test_dispatches_to_matching_config(self):
        from src.core.crypto import encrypt_apprise_url
        from src.core.models.notification_config import WatchNotificationConfig
        from src.core.notifications.dispatcher import DispatchResult

        watch_ulid = ULID()
        config = MagicMock(spec=WatchNotificationConfig)
        config.id = ULID()
        config.watch_id = watch_ulid
        config.apprise_url = encrypt_apprise_url("json://localhost/notify")
        config.events = ["change_detected"]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[self._result_with(config), self._empty_result()])

        event = make_event(watch_id=str(watch_ulid))

        dispatch_result = DispatchResult(success=True, reason="ok")
        with patch(
            "src.workers.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=dispatch_result,
        ):
            await dispatch_event_notifications(session, event)

        session.add.assert_called_once()  # audit log entry added

    async def test_failure_does_not_raise(self):
        from src.core.crypto import encrypt_apprise_url
        from src.core.models.notification_config import WatchNotificationConfig

        watch_ulid = ULID()
        config = MagicMock(spec=WatchNotificationConfig)
        config.id = ULID()
        config.watch_id = watch_ulid
        config.apprise_url = encrypt_apprise_url("json://localhost/notify")
        config.events = ["change_detected"]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[self._result_with(config), self._empty_result()])

        event = make_event(watch_id=str(watch_ulid))

        with patch(
            "src.workers.notify.dispatch_event",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            # Should not raise
            await dispatch_event_notifications(session, event)
