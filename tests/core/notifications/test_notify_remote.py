"""Tests for ``dispatch_via_notifier`` — one candidate, one notifier call.

After Phase 5 (#137) the notifier service is the only delivery path. Since F3
(#376) Watcher dispatches no event of its own; this call serves the operator's
test-send routes until #379 deletes them. These tests assert the notifier-API
contract per candidate: channel, idempotency key, metadata, FAILED-status
reasons, and a ``NotifierError`` caught rather than raised.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from notifier_client.errors import NotifierError
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import DispatchCandidate, dispatch_via_notifier


def _make_event(event_type=WatchEventType.CHANGE_DETECTED):
    return WatchEvent(
        event_type=event_type,
        watched_item_id=str(ULID()),
        item_name="Test Watch",
        item_url="https://example.com",
        occurred_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        metadata={},
    )


def _candidate(source="watched_item"):
    return DispatchCandidate(source=source, source_id=str(ULID()), remote_channel_id=str(ULID()))


def _make_dispatch_out(status="succeeded", attempt_reasons=()):
    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED if status == "succeeded" else DispatchOutStatus.FAILED
    out.attempts = [MagicMock(reason=r) for r in attempt_reasons]
    return out


def _client(dispatch_return=None, dispatch_side_effect=None):
    client = AsyncMock()
    if dispatch_side_effect is not None:
        client.dispatch = AsyncMock(side_effect=dispatch_side_effect)
    else:
        client.dispatch = AsyncMock(return_value=dispatch_return or _make_dispatch_out())
    return client


async def _send(client, candidate=None, event=None):
    return await dispatch_via_notifier(
        client, candidate or _candidate(), event or _make_event(), "title", "body"
    )


class TestDispatchViaNotifier:
    async def test_success_sends_to_the_candidates_channel(self):
        candidate = _candidate()
        out = _make_dispatch_out("succeeded")
        client = _client(dispatch_return=out)

        result = await _send(client, candidate)

        assert result.success is True
        assert result.reason == f"notifier:{out.id}"
        kwargs = client.dispatch.call_args.kwargs
        assert kwargs["channel_ids"] == [candidate.remote_channel_id]
        assert "watcher:change_detected:" in kwargs["idempotency_key"]
        assert (kwargs["title_template"], kwargs["body_template"]) == ("title", "body")

    async def test_metadata_includes_event_type_and_source(self):
        client = _client()

        await _send(client, _candidate("global"), _make_event(WatchEventType.WATCH_ERROR))

        metadata = client.dispatch.call_args.kwargs["metadata"]
        assert metadata["event_type"] == "watch_error"
        assert metadata["source"] == "global"

    async def test_notifier_error_is_a_failure_not_a_raise(self):
        error = NotifierError("server error", status_code=500, response=MagicMock())
        client = _client(dispatch_side_effect=error)

        result = await _send(client)

        assert result.success is False
        assert "notifier" in result.reason.lower()

    async def test_failed_status_takes_the_attempts_reason(self):
        client = _client(dispatch_return=_make_dispatch_out("failed", ["channel unreachable"]))

        result = await _send(client)

        assert (result.success, result.reason) == (False, "channel unreachable")

    async def test_failed_status_without_attempts_uses_the_default_reason(self):
        client = _client(dispatch_return=_make_dispatch_out("failed"))

        result = await _send(client)

        assert (result.success, result.reason) == (False, "Delivery failed via notifier")

    async def test_failed_status_with_a_null_reason_uses_the_default_reason(self):
        client = _client(dispatch_return=_make_dispatch_out("failed", [None]))

        result = await _send(client)

        assert (result.success, result.reason) == (False, "Delivery failed via notifier")
