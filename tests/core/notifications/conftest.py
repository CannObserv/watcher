"""Shared fixtures for notification tests."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType


@pytest.fixture
def make_event():
    """Factory fixture: build a WatchEvent with sensible defaults."""

    def _make(event_type=WatchEventType.CHANGE_DETECTED, **overrides):
        defaults = {
            "event_type": event_type,
            "watch_id": "01HV0000000000000000000001",
            "watch_name": "Test Watch",
            "watch_url": "https://example.com",
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "metadata": {
                "added": ["Page 2", "Page 3"],
                "modified": ["Page 1"],
                "removed": [],
            },
        }
        defaults.update(overrides)
        return WatchEvent(**defaults)

    return _make
