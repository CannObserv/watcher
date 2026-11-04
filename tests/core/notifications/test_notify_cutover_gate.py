"""The cutover gate in ``dispatch_event_notifications`` (#376 part 1, F3).

With ``WATCHER_NOTIFY_CUTOVER_AT`` set, an event whose ``occurred_at`` is at or
after T is Archiver's: the dispatcher logs one line and returns, before it
looks up a template, builds a client or writes an audit. Unset, nothing
changes. The boundary matches Archiver's ``occurred_at >= T`` exactly, so an
event is sent by one service and never by both or neither.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.notify_cutover import NOTIFY_CUTOVER_AT_ENV

T = datetime(2026, 11, 6, 21, 0, 0, tzinfo=UTC)
T_ENV = "2026-11-06T21:00:00Z"
ITEM_URL = "https://example.com/secret-path?token=abc"


def _event(occurred_at: datetime, event_type=WatchEventType.CHANGE_DETECTED, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watched_item_id=str(ULID()),
        item_name="Test Watch",
        item_url=ITEM_URL,
        occurred_at=occurred_at,
        metadata=metadata or {},
    )


def _template():
    tpl = MagicMock()
    tpl.id = ULID()
    tpl.visibility = "global"
    tpl.events = [e.value for e in WatchEventType]
    tpl.content_config = None
    tpl.remote_channel_id = str(ULID())
    return tpl


def _session():
    session = AsyncMock()
    wi = MagicMock()
    wi.domain_name = None
    session.get = AsyncMock(return_value=wi)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_template()]
    session.execute = AsyncMock(return_value=result)
    return session


def _client():
    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED
    out.attempts = []
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.dispatch = AsyncMock(return_value=out)
    return client


async def _dispatch(event):
    """Run the dispatcher; return (session, get_notifier_client mock, audit mock)."""
    session = _session()
    client = _client()
    with (
        patch(
            "src.core.notifications.notify.get_notifier_client", return_value=client
        ) as get_client,
        patch("src.core.notifications.notify.audit") as audit,
    ):
        await dispatch_event_notifications(session=session, event=event)
    return session, get_client, client, audit


class TestUnset:
    async def test_unset_dispatches_as_today(self, monkeypatch):
        monkeypatch.delenv(NOTIFY_CUTOVER_AT_ENV, raising=False)
        _, _, client, audit = await _dispatch(_event(T + timedelta(days=365)))
        client.dispatch.assert_called_once()
        audit.assert_called_once()

    async def test_blank_dispatches_as_today(self, monkeypatch):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, "")
        _, _, client, audit = await _dispatch(_event(T + timedelta(days=365)))
        client.dispatch.assert_called_once()
        audit.assert_called_once()


class TestBoundary:
    async def test_before_t_is_watchers(self, monkeypatch):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        _, _, client, audit = await _dispatch(_event(T - timedelta(microseconds=1)))
        client.dispatch.assert_called_once()
        audit.assert_called_once()

    @pytest.mark.parametrize("offset", [timedelta(0), timedelta(microseconds=1), timedelta(days=1)])
    async def test_at_or_after_t_is_archivers(self, monkeypatch, offset):
        """``occurred_at == T`` included: Archiver sends ``>= T``."""
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        session, get_client, client, audit = await _dispatch(_event(T + offset))
        get_client.assert_not_called()
        client.dispatch.assert_not_called()
        audit.assert_not_called()
        session.execute.assert_not_called()

    async def test_the_gate_compares_instants_not_spellings(self, monkeypatch):
        """``+00:00`` is the same T as ``Z``."""
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, "2026-11-06T21:00:00+00:00")
        _, _, client, _ = await _dispatch(_event(T))
        client.dispatch.assert_not_called()


class TestEveryEventType:
    @pytest.mark.parametrize("event_type", list(WatchEventType))
    async def test_each_type_is_suppressed_after_t(self, monkeypatch, event_type):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        _, _, client, audit = await _dispatch(_event(T, event_type=event_type))
        client.dispatch.assert_not_called()
        audit.assert_not_called()

    async def test_a_reminder_after_t_is_suppressed(self, monkeypatch):
        """A #71 reminder is a ``watch_error`` with ``renotify: true`` and has no
        fact of its own; after T the reminder cadence is Archiver's (D5)."""
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        reminder = _event(
            T + timedelta(hours=24),
            event_type=WatchEventType.WATCH_ERROR,
            metadata={"renotify": True, "previously_notified_at": "2026-11-05T21:00:00Z"},
        )
        _, get_client, client, audit = await _dispatch(reminder)
        get_client.assert_not_called()
        client.dispatch.assert_not_called()
        audit.assert_not_called()


class TestSuppressionLine:
    async def test_one_line_with_type_item_and_both_instants(self, monkeypatch, caplog):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        event = _event(T, event_type=WatchEventType.WATCH_RECOVERED)
        with caplog.at_level(logging.INFO, logger="src.core.notifications.notify"):
            await _dispatch(event)
        lines = [r for r in caplog.records if r.getMessage() == "notification suppressed: cutover"]
        assert len(lines) == 1
        record = lines[0]
        assert record.event_type == "watch_recovered"
        assert record.watched_item_id == event.watched_item_id
        assert record.occurred_at == "2026-11-06T21:00:00.000000Z"
        assert record.cutover_at == "2026-11-06T21:00:00.000000Z"

    async def test_the_line_never_carries_a_url(self, monkeypatch, caplog):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        with caplog.at_level(logging.DEBUG):
            await _dispatch(_event(T))
        for record in caplog.records:
            assert "example.com" not in json.dumps(record.__dict__, default=str)

    async def test_before_t_logs_no_suppression(self, monkeypatch, caplog):
        monkeypatch.setenv(NOTIFY_CUTOVER_AT_ENV, T_ENV)
        with caplog.at_level(logging.INFO, logger="src.core.notifications.notify"):
            await _dispatch(_event(T - timedelta(seconds=1)))
        assert not any("suppressed" in r.getMessage() for r in caplog.records)
