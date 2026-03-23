"""Shared fixtures for notification channel tests."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.base import ChangeEvent


@pytest.fixture
def make_event():
    """Factory fixture: build a ChangeEvent with sensible defaults."""

    def _make(**overrides):
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

    return _make
