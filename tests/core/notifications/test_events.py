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
        watched_item_id="01HV0000000000000000000001",
        item_name="Test Watch",
        item_url="https://example.com",
        occurred_at=OCCURRED_AT,
        metadata=metadata or {},
    )


class TestWatchEventType:
    def test_exactly_the_dispatched_types_exist(self):
        """Only events with a dispatch site are members (#166) — the guard
        tying each to its call site is `tests/test_notification_dispatch_sites.py`."""
        assert {e.value for e in WatchEventType} == {
            "change_detected",
            "watch_error",
            "watch_recovered",
        }

    @pytest.mark.parametrize(
        "dropped",
        ["watch_created", "watch_paused", "watch_resumed", "watch_archived", "watch_deleted"],
    )
    def test_never_firing_types_are_gone(self, dropped):
        """#166: five members were subscribable but never dispatched."""
        with pytest.raises(ValueError):
            WatchEventType(dropped)

    def test_is_str_enum(self):
        assert WatchEventType.CHANGE_DETECTED == "change_detected"


class TestEventTitles:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in EVENT_TITLES, f"EVENT_TITLES missing {et.value}"

    def test_titles_are_human_readable(self):
        # #221: labels dropped the "Watch" prefix; change_detected → "Change".
        assert EVENT_TITLES["change_detected"] == "Change"
        assert EVENT_TITLES["watch_error"] == "Error"

    def test_titles_carry_no_watch_prefix(self):
        for label in EVENT_TITLES.values():
            assert not label.startswith("Watch "), f"{label!r} still carries Watch prefix"

    def test_iteration_order_is_temporal(self):
        """EVENT_TITLES iterates in roughly temporal lifecycle order.

        Drives the Subscribe checkbox order in the notification form
        (templates iterate `event_titles.items()`).
        """
        assert list(EVENT_TITLES.keys()) == [
            "change_detected",
            "watch_error",
            "watch_recovered",
        ]

    def test_watch_event_type_iteration_order_matches(self):
        """WatchEventType declaration order matches EVENT_TITLES order."""
        assert [et.value for et in WatchEventType] == list(EVENT_TITLES.keys())


class TestWatchEventImmutable:
    def test_frozen(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        with pytest.raises(Exception):
            event.watched_item_id = "other"
