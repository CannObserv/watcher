"""Tests for the Apprise-based notification dispatcher."""

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apprise import NotifyFormat
from cryptography.fernet import Fernet

from src.core.notifications.dispatcher import _ASSET, dispatch_event
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


class TestAppriseAsset:
    def test_asset_app_id(self):
        assert _ASSET.app_id == "CO Watcher"

    def test_asset_images_suppressed(self):
        assert _ASSET.image_url_mask == ""
        assert _ASSET.image_url_logo == ""


class TestDispatchEvent:
    async def test_apprise_constructed_with_asset(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            await dispatch_event(event, encrypted, body="test body", title="test title")

        MockApprise.assert_called_once_with(asset=_ASSET)

    async def test_returns_true_on_apprise_success(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is True
        assert result.reason
        instance.async_notify.assert_awaited_once()

    async def test_returns_failure_on_apprise_failure(self):
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=False)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is False
        assert "rejected" in result.reason.lower() or "delivery" in result.reason.lower()

    async def test_returns_failure_on_apprise_none(self):
        """None from async_notify means nothing was dispatched."""
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=None)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is False

    async def test_returns_failure_on_invalid_url(self):
        """add() returning False means Apprise rejected the URL."""
        event = make_event()
        encrypted = make_encrypted_url("notaschema://whatever")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = False
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is False
        assert "invalid" in result.reason.lower()

    async def test_passes_correct_notify_type(self):
        event = make_event(WatchEventType.WATCH_ERROR, metadata={"status_code": 500})
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            await dispatch_event(event, encrypted, body="test body", title="test title")

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

            await dispatch_event(event, encrypted, body="rendered body", title="rendered title")

        call_kwargs = instance.async_notify.call_args.kwargs
        assert call_kwargs["title"] == "rendered title"
        assert call_kwargs["body"] == "rendered body"

    async def test_failure_reason_includes_apprise_log_detail(self):
        """WARNING emitted by apprise logger during async_notify appears in reason."""
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")
        apprise_logger = logging.getLogger("apprise")

        async def fake_notify(**kwargs):
            apprise_logger.warning("not_in_channel: {'ok': False, 'error': 'not_in_channel'}")
            return False

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(side_effect=fake_notify)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is False
        assert "not_in_channel" in result.reason

    async def test_success_does_not_include_captured_logs(self):
        """Warnings emitted during a successful dispatch do not leak into reason."""
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")
        apprise_logger = logging.getLogger("apprise")

        async def fake_notify(**kwargs):
            apprise_logger.warning("some harmless warning")
            return True

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(side_effect=fake_notify)
            MockApprise.return_value = instance

            result = await dispatch_event(event, encrypted, body="test body", title="test title")

        assert result.success is True
        assert "harmless warning" not in result.reason

    async def test_concurrent_dispatch_logs_not_cross_contaminated(self):
        """Concurrent dispatch_event calls capture only their own apprise log lines."""
        apprise_logger = logging.getLogger("apprise")
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        def make_notifier(msg: str):
            async def fake_notify(**kwargs):
                await asyncio.sleep(0)  # yield to allow interleaving
                apprise_logger.warning(msg)
                return False

            return fake_notify

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            inst1, inst2 = MagicMock(), MagicMock()
            inst1.add.return_value = True
            inst2.add.return_value = True
            inst1.async_notify = AsyncMock(side_effect=make_notifier("error_for_call_1"))
            inst2.async_notify = AsyncMock(side_effect=make_notifier("error_for_call_2"))
            MockApprise.side_effect = [inst1, inst2]

            r1, r2 = await asyncio.gather(
                dispatch_event(event, encrypted, body="b1", title="t1"),
                dispatch_event(event, encrypted, body="b2", title="t2"),
            )

        assert "error_for_call_1" in r1.reason
        assert "error_for_call_1" not in r2.reason
        assert "error_for_call_2" in r2.reason
        assert "error_for_call_2" not in r1.reason

    async def test_passes_body_format_markdown_for_apprise_downconversion(self):
        """body_format=MARKDOWN always passed; Apprise downconverts the body
        for HTML and plaintext channels (issue #116). Markdown gives us a
        single canonical body — fenced ```diff blocks render cleanly on
        Discord/Slack/email and degrade readably on plaintext channels."""
        event = make_event()
        encrypted = make_encrypted_url("json://localhost/notify")

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(return_value=True)
            MockApprise.return_value = instance

            await dispatch_event(event, encrypted, body="line one\nline two", title="test title")

        call_kwargs = instance.async_notify.call_args.kwargs
        assert call_kwargs["body_format"] == NotifyFormat.MARKDOWN

    async def test_dispatch_event_passes_body_and_title_verbatim(self):
        """body and title are required kwargs and forwarded to Apprise unchanged."""
        event = make_event()
        encrypted = make_encrypted_url("slack://T/A/B")

        captured = {}

        async def fake_notify(**kwargs):
            captured["body"] = kwargs.get("body")
            captured["title"] = kwargs.get("title")
            return True

        with patch("src.core.notifications.dispatcher.apprise.Apprise") as MockApprise:
            instance = MagicMock()
            instance.add.return_value = True
            instance.async_notify = AsyncMock(side_effect=fake_notify)
            MockApprise.return_value = instance

            result = await dispatch_event(
                event, encrypted, body="Custom body text", title="Custom title"
            )

        assert result.success is True
        assert captured["body"] == "Custom body text"
        assert captured["title"] == "Custom title"
