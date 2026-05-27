"""Tests for the simplified remote-only dispatch path.

Phase 5 (#137): the local Apprise dispatch path was removed. The notifier
service is now the only delivery mechanism. These tests assert that:

  - `dispatch_event_notifications` no longer reads `USE_REMOTE_NOTIFY`
  - it always opens a notifier client + calls `dispatch_via_notifier`
  - the local `dispatch_event` symbol is not importable from `notify`
  - `DispatchCandidate` no longer carries an `apprise_url` field
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import DispatchCandidate, dispatch_event_notifications


def _make_event(event_type=WatchEventType.CHANGE_DETECTED):
    return WatchEvent(
        event_type=event_type,
        watch_id=str(ULID()),
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        metadata={},
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
        r.one_or_none.return_value = (domain, wid)
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


def _make_dispatch_out():
    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED
    out.attempts = []
    return out


def _mock_notifier_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.dispatch = AsyncMock(return_value=_make_dispatch_out())
    return client


class TestRemoteOnlyDispatch:
    async def test_dispatch_candidate_has_no_apprise_url_field(self):
        """The local Apprise URL is gone — the dataclass shouldn't carry it."""
        candidate = DispatchCandidate(
            source="local",
            source_id=str(ULID()),
            remote_channel_id=str(ULID()),
        )
        assert not hasattr(candidate, "apprise_url")

    async def test_no_use_remote_notify_env_check(self, monkeypatch):
        """Even with USE_REMOTE_NOTIFY unset, the notifier client is still used.

        Regression guard for the Phase 5 strip: the env flag must not gate
        dispatch any longer.
        """
        monkeypatch.delenv("USE_REMOTE_NOTIFY", raising=False)
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        remote_id = str(ULID())
        local_cfg = _fake_local_config(remote_channel_id=remote_id)
        event = _make_event()

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

        mock_client = _mock_notifier_client()

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        mock_client.dispatch.assert_called_once()

    async def test_no_local_dispatch_event_import(self):
        """The legacy `dispatch_event` symbol must no longer live in notify.py.

        It was the entry-point for the local Apprise path. Removing it ensures
        nothing in this module can fall back to local dispatch.
        """
        import src.core.notifications.notify as notify

        assert not hasattr(notify, "dispatch_event"), (
            "dispatch_event (local Apprise path) should be removed from notify.py"
        )

    async def test_candidate_missing_remote_channel_id_logs_and_skips(self, monkeypatch):
        """Without a remote_channel_id there's no fallback — record the failure.

        Pre-strip behaviour: log a warning and dispatch via local Apprise.
        Post-strip: log + record an unsuccessful audit result. No exception.
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

        captured = []

        def _capture(session, event_type, **kwargs):
            captured.extend(kwargs.get("results", []))

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=mock_client),
            patch("src.core.notifications.notify.audit", side_effect=_capture),
        ):
            await dispatch_event_notifications(session=session, event=event)

        # Notifier API was NOT called (no channel id to dispatch to).
        mock_client.dispatch.assert_not_called()
        # The candidate was recorded as a failure with a clear reason.
        assert len(captured) == 1
        assert captured[0]["success"] is False
        assert "remote_channel_id" in captured[0]["reason"]


@pytest.mark.parametrize(
    "removed_symbol",
    ["nullcontext"],
)
async def test_removed_imports_from_notify(removed_symbol):
    """`from contextlib import nullcontext` was only needed to gate the
    notifier client behind USE_REMOTE_NOTIFY. After the strip, it should
    no longer be imported in notify.py."""
    import src.core.notifications.notify as notify

    assert not hasattr(notify, removed_symbol), (
        f"{removed_symbol} should be removed from notify.py after the apprise strip"
    )
