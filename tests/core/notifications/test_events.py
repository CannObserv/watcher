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
