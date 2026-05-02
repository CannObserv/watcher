"""Tests for the USE_REMOTE_NOTIFY dispatch path in dispatch_event_notifications."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from ulid import ULID

from src.core.notifications.dispatcher import DispatchResult
from src.core.notifications.events import WatchEvent, WatchEventType


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def _make_event(event_type=WatchEventType.CHANGE_DETECTED, *, change_id=None):
    metadata = {}
    if change_id:
        metadata["change_id"] = change_id
    return WatchEvent(
        event_type=event_type,
        watch_id=str(ULID()),
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        metadata=metadata,
    )


def _watch_meta_result(domain=None):
    r = MagicMock()
    r.one_or_none.return_value = None if domain is None else (domain, None)
    return r


def _empty_result():
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    return r


def _result_with(*items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _fake_local_config(remote_channel_id=None):
    from src.core.crypto import encrypt_apprise_url

    c = MagicMock()
    c.id = ULID()
    c.apprise_url = encrypt_apprise_url("json://local.example.com/notify")
    c.events = ["change_detected"]
    c.content_config = None
    c.remote_channel_id = remote_channel_id
    return c


def _make_dispatch_out(status="succeeded"):
    from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus

    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED if status == "succeeded" else DispatchOutStatus.FAILED
    out.attempts = []
    return out


class TestRemoteDispatchPath:
    async def test_uses_notifier_when_flag_on_and_channel_migrated(self, monkeypatch):
        """With USE_REMOTE_NOTIFY=1 and remote_channel_id set, calls notifier API."""
        monkeypatch.setenv("USE_REMOTE_NOTIFY", "1")
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event(change_id=str(ULID()))

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),  # global
                _empty_result(),  # watch templates
                _result_with(local_cfg),  # local
            ]
        )

        mock_client = AsyncMock()
        mock_client.dispatch = AsyncMock(return_value=_make_dispatch_out("succeeded"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            from src.core.notifications.notify import dispatch_event_notifications

            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_called_once()
        call_kwargs = mock_client.dispatch.call_args.kwargs
        assert call_kwargs["channel_ids"] == [remote_id]
        assert "watcher:change_detected:" in call_kwargs["idempotency_key"]

    async def test_falls_back_to_local_when_flag_on_but_no_remote_id(self, monkeypatch):
        """With USE_REMOTE_NOTIFY=1 but remote_channel_id=None, falls back to local Apprise."""
        monkeypatch.setenv("USE_REMOTE_NOTIFY", "1")
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        local_cfg = _fake_local_config(remote_channel_id=None)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = AsyncMock()
        mock_client.dispatch = AsyncMock(return_value=_make_dispatch_out())

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch(
                "src.core.notifications.notify.dispatch_event",
                new_callable=AsyncMock,
                return_value=DispatchResult(success=True, reason="ok"),
            ) as mock_local,
            patch("src.core.notifications.notify.audit"),
        ):
            from src.core.notifications.notify import dispatch_event_notifications

            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_not_called()
        mock_local.assert_called_once()

    async def test_uses_local_dispatch_when_flag_off(self, monkeypatch):
        """With USE_REMOTE_NOTIFY=0 (default), always uses local Apprise dispatcher."""
        monkeypatch.setenv("USE_REMOTE_NOTIFY", "0")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = AsyncMock()
        mock_client.dispatch = AsyncMock(return_value=_make_dispatch_out())

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch(
                "src.core.notifications.notify.dispatch_event",
                new_callable=AsyncMock,
                return_value=DispatchResult(success=True, reason="ok"),
            ) as mock_local,
            patch("src.core.notifications.notify.audit"),
        ):
            from src.core.notifications.notify import dispatch_event_notifications

            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_not_called()
        mock_local.assert_called_once()

    async def test_notifier_error_recorded_as_failure(self, monkeypatch):
        """NotifierError from the API is caught and logged as a failed attempt."""
        monkeypatch.setenv("USE_REMOTE_NOTIFY", "1")
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        from notifier_client.errors import NotifierError

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = AsyncMock()
        mock_client.dispatch = AsyncMock(
            side_effect=NotifierError("server error", status_code=500, response=MagicMock())
        )

        results_captured = []

        def capture_audit(session, event_type, **kwargs):
            results_captured.extend(kwargs.get("results", []))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            from src.core.notifications.notify import dispatch_event_notifications

            await dispatch_event_notifications(session=session, event=event)

        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert "notifier" in results_captured[0]["reason"].lower()

    async def test_metadata_includes_event_type_and_source(self, monkeypatch):
        """dispatch() is called with metadata containing event_type and source fields."""
        monkeypatch.setenv("USE_REMOTE_NOTIFY", "1")
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event(WatchEventType.WATCH_CREATED)

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = AsyncMock()
        mock_client.dispatch = AsyncMock(return_value=_make_dispatch_out("succeeded"))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            from src.core.notifications.notify import dispatch_event_notifications

            await dispatch_event_notifications(session=session, event=event)

        call_kwargs = mock_client.dispatch.call_args.kwargs
        metadata = call_kwargs["metadata"]
        assert metadata["event_type"] == "watch_created"
        assert metadata["source"] == "local"
