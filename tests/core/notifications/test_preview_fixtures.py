"""Tests for the notification preview mock-event fixtures."""

from datetime import datetime

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.preview_fixtures import (
    MOCK_EVENT_FIXTURES,
    build_preview_event,
)


class TestMockEventFixtures:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in MOCK_EVENT_FIXTURES, f"MOCK_EVENT_FIXTURES missing {et.value}"

    def test_change_detected_has_diff_metadata(self):
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        assert "added" in fx
        assert "modified" in fx
        assert "removed" in fx

    def test_watch_error_has_status_code(self):
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert "status_code" in fx


class TestBuildPreviewEvent:
    def test_returns_watchevent_for_every_event_type(self):
        for et in WatchEventType:
            ev = build_preview_event(et.value)
            assert isinstance(ev, WatchEvent)
            assert ev.event_type == et

    def test_event_has_watch_name_and_url(self):
        ev = build_preview_event("change_detected")
        assert ev.watch_name
        assert ev.watch_url.startswith("http")

    def test_event_has_occurred_at(self):
        ev = build_preview_event("change_detected")
        assert isinstance(ev.occurred_at, datetime)

    def test_metadata_includes_fixture_fields(self):
        ev = build_preview_event("change_detected")
        assert "added" in ev.metadata
        assert "modified" in ev.metadata
        assert "significance" in ev.metadata

    def test_unknown_event_type_raises(self):
        with pytest.raises(KeyError):
            build_preview_event("not_a_real_event_type")
