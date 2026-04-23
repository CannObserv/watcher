"""Tests for WatchEvent and WatchEventType.

Title and body rendering tests live in `test_content.py` — they are now
composed from Jinja templates in `default_templates.py`, not computed on the
WatchEvent itself.
"""

from datetime import UTC, datetime

import pytest

from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType

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
        assert "watch_archived" in codes
        assert "watch_deleted" in codes

    def test_is_str_enum(self):
        assert WatchEventType.CHANGE_DETECTED == "change_detected"


class TestEventTitles:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in EVENT_TITLES, f"EVENT_TITLES missing {et.value}"

    def test_titles_are_human_readable(self):
        assert EVENT_TITLES["change_detected"] == "Change Detected"
        assert EVENT_TITLES["watch_error"] == "Watch Error"

    def test_iteration_order_is_temporal(self):
        """EVENT_TITLES iterates in roughly temporal lifecycle order.

        Drives the Subscribe checkbox order in the notification form
        (templates iterate `event_titles.items()`).
        """
        assert list(EVENT_TITLES.keys()) == [
            "watch_created",
            "change_detected",
            "watch_error",
            "watch_recovered",
            "watch_paused",
            "watch_resumed",
            "watch_archived",
            "watch_deleted",
        ]

    def test_watch_event_type_iteration_order_matches(self):
        """WatchEventType declaration order matches EVENT_TITLES order."""
        assert [et.value for et in WatchEventType] == list(EVENT_TITLES.keys())


class TestWatchEventImmutable:
    def test_frozen(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        with pytest.raises(Exception):
            event.watch_id = "other"


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

    def test_watch_archived_is_warning(self):
        event = make_event(WatchEventType.WATCH_ARCHIVED)
        assert event.apprise_notify_type == "warning"

    def test_watch_deleted_is_warning(self):
        event = make_event(WatchEventType.WATCH_DELETED)
        assert event.apprise_notify_type == "warning"
