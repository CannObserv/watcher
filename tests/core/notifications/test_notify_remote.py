"""Tests for the remote dispatch path in dispatch_event_notifications.

After Phase 5 (#137), this is the only dispatch path — there is no local
Apprise fallback. These tests assert notifier-API integration behaviour
(idempotency keys, FAILED-status handling, error catching).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from notifier_client.errors import NotifierError
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications


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


def _watch_meta_result(domain=None, watched_item_id=None, *, missing=False):
    """Default: a watch exists with the given domain + a synthetic watched_item_id.

    Pass ``missing=True`` to simulate Watch.one_or_none() returning None (no row).
    """
    r = MagicMock()
    if missing:
        r.one_or_none.return_value = None
    else:
        wid = watched_item_id if watched_item_id is not None else ULID()
        r.one_or_none.return_value = (domain, None, wid)
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
    c = MagicMock()
    c.id = ULID()
    c.events = ["change_detected"]
    c.content_config = None
    c.remote_channel_id = remote_channel_id
    return c


def _fake_template(remote_channel_id=None):
    """Fake WatchedItemNotificationTemplate row (same shape as local config)."""
    t = MagicMock()
    t.id = ULID()
    t.events = ["change_detected"]
    t.content_config = None
    t.remote_channel_id = remote_channel_id
    return t


def _make_dispatch_out(status="succeeded"):
    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED if status == "succeeded" else DispatchOutStatus.FAILED
    out.attempts = []
    return out


def _mock_notifier_client(dispatch_return=None, dispatch_side_effect=None):
    """Return an AsyncMock that satisfies `async with get_notifier_client() as client:`."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if dispatch_side_effect is not None:
        client.dispatch = AsyncMock(side_effect=dispatch_side_effect)
    else:
        client.dispatch = AsyncMock(return_value=dispatch_return or _make_dispatch_out())
    return client


def _make_audit_capture():
    """Return (side_effect_fn, results_list) for patching audit in failure-mode tests."""
    captured = []

    def _capture(session, event_type, **kwargs):
        captured.extend(kwargs.get("results", []))

    return _capture, captured


class TestRemoteDispatchPath:
    async def test_uses_notifier_when_remote_channel_id_set(self, monkeypatch):
        """With remote_channel_id set, calls the notifier API."""
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
                _empty_result(),  # watched_item templates
                _result_with(local_cfg),  # local
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=_make_dispatch_out("succeeded"))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_called_once()
        call_kwargs = mock_client.dispatch.call_args.kwargs
        assert call_kwargs["channel_ids"] == [remote_id]
        assert "watcher:change_detected:" in call_kwargs["idempotency_key"]

    async def test_no_remote_channel_id_records_failure(self, monkeypatch):
        """A candidate without a remote_channel_id is logged as failed.

        Previously this fell back to local Apprise; that path is gone in #137.
        """
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
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client()

        capture_audit, results_captured = _make_audit_capture()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_not_called()
        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert "remote_channel_id" in results_captured[0]["reason"]

    async def test_notifier_error_recorded_as_failure(self, monkeypatch):
        """NotifierError from the API is caught and logged as a failed attempt."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client(
            dispatch_side_effect=NotifierError(
                "server error", status_code=500, response=MagicMock()
            )
        )

        capture_audit, results_captured = _make_audit_capture()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            await dispatch_event_notifications(session=session, event=event)

        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert "notifier" in results_captured[0]["reason"].lower()

    async def test_metadata_includes_event_type_and_source(self, monkeypatch):
        """dispatch() is called with metadata containing event_type and source fields."""
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
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=_make_dispatch_out("succeeded"))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        call_kwargs = mock_client.dispatch.call_args.kwargs
        metadata = call_kwargs["metadata"]
        assert metadata["event_type"] == "watch_created"
        assert metadata["source"] == "local"

    async def test_notifier_failed_status_recorded_as_failure(self, monkeypatch):
        """FAILED status from notifier API is success=False; reason taken from attempts."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        failed_out = _make_dispatch_out("failed")
        attempt = MagicMock()
        attempt.reason = "channel unreachable"
        failed_out.attempts = [attempt]

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=failed_out)

        capture_audit, results_captured = _make_audit_capture()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            await dispatch_event_notifications(session=session, event=event)

        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert results_captured[0]["reason"] == "channel unreachable"

    async def test_notifier_failed_status_uses_default_reason_when_no_attempts(self, monkeypatch):
        """FAILED status with no attempts falls back to the default reason string."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=_make_dispatch_out("failed"))

        capture_audit, results_captured = _make_audit_capture()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            await dispatch_event_notifications(session=session, event=event)

        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert results_captured[0]["reason"] == "Delivery failed via notifier"

    async def test_watched_item_template_is_dispatched(self, monkeypatch):
        """A WatchedItemNotificationTemplate row reaches the notifier (#160 Task 9)."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        template = _fake_template(remote_channel_id=remote_id)
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),  # global
                _empty_result(),  # watch templates (WatchNcRef)
                _result_with(template),  # watched_item templates
                _empty_result(),  # local
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=_make_dispatch_out("succeeded"))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_called_once()
        call_kwargs = mock_client.dispatch.call_args.kwargs
        assert call_kwargs["channel_ids"] == [remote_id]
        assert call_kwargs["metadata"]["source"] == "watched_item_template"

    async def test_watched_item_template_and_local_both_dispatched(self, monkeypatch):
        """Union: 1 WatchedItem template + 1 local config = 2 dispatches (#160 Task 9)."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        template = _fake_template(remote_channel_id=str(ULID()))
        local_cfg = _fake_local_config(remote_channel_id=str(ULID()))
        event = _make_event()

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),  # global
                _empty_result(),  # watch templates (WatchNcRef)
                _result_with(template),  # watched_item templates
                _result_with(local_cfg),  # local
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=_make_dispatch_out("succeeded"))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        assert mock_client.dispatch.call_count == 2
        sources = {
            call.kwargs["metadata"]["source"] for call in mock_client.dispatch.call_args_list
        }
        assert sources == {"watched_item_template", "local"}

    async def test_notifier_failed_status_uses_default_reason_when_attempt_reason_is_none(
        self, monkeypatch
    ):
        """attempt.reason=None falls back to the default via `or reason`."""
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

        failed_out = _make_dispatch_out("failed")
        attempt = MagicMock()
        attempt.reason = None
        failed_out.attempts = [attempt]

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
                _result_with(local_cfg),
            ]
        )

        mock_client = _mock_notifier_client(dispatch_return=failed_out)

        capture_audit, results_captured = _make_audit_capture()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=capture_audit),
        ):
            await dispatch_event_notifications(session=session, event=event)

        assert len(results_captured) == 1
        assert results_captured[0]["success"] is False
        assert results_captured[0]["reason"] == "Delivery failed via notifier"
