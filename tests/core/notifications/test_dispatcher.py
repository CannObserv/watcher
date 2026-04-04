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
