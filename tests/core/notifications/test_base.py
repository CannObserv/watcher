"""Tests for ChangeEvent and NotificationChannel protocol."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.base import ChangeEvent, NotificationChannel


class TestChangeEvent:
    """ChangeEvent dataclass behaviour."""

    def _make_event(self, **overrides):
        defaults = {
            "watch_id": "w1",
            "watch_name": "Test Watch",
            "watch_url": "https://example.com",
            "change_id": "c1",
            "detected_at": datetime(2026, 1, 1, tzinfo=UTC),
            "change_metadata": {
                "added": ["Page 2", "Page 3"],
                "modified": ["Page 1"],
                "removed": [],
            },
        }
        defaults.update(overrides)
        return ChangeEvent(**defaults)

    def test_frozen(self):
        event = self._make_event()
        with pytest.raises(AttributeError):
            event.watch_name = "other"  # type: ignore[misc]

    def test_summary_with_counts(self):
        event = self._make_event(
            change_metadata={
                "added": ["Page 2", "Page 3", "Page 4"],
                "modified": ["Page 1"],
                "removed": ["Page 5", "Page 6"],
            }
        )
        assert event.summary == "Change detected: Test Watch — 3 added, 1 modified, 2 removed"

    def test_summary_skips_zero_counts(self):
        event = self._make_event(
            change_metadata={
                "added": [],
                "modified": ["Section A", "Section B", "Section C", "Section D", "Section E"],
                "removed": [],
            }
        )
        assert event.summary == "Change detected: Test Watch — 5 modified"

    def test_summary_empty_metadata(self):
        event = self._make_event(change_metadata={})
        assert "details pending" in event.summary

    def test_default_change_metadata(self):
        event = ChangeEvent(
            watch_id="w1",
            watch_name="W",
            watch_url="https://example.com",
            change_id="c1",
            detected_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert event.change_metadata == {}


class TestNotificationChannelProtocol:
    """NotificationChannel is a runtime-checkable Protocol."""

    def test_class_satisfying_protocol(self):
        class _Good:
            async def send(self, event: ChangeEvent, config: dict) -> bool:
                return True

        assert isinstance(_Good(), NotificationChannel)

    def test_class_missing_send(self):
        class _Bad:
            pass

        assert not isinstance(_Bad(), NotificationChannel)
