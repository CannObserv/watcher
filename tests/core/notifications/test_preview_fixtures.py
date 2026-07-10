"""Tests for the notification preview mock-event fixtures."""

from datetime import UTC, datetime

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.preview_fixtures import (
    MOCK_EVENT_FIXTURES,
    build_preview_event,
)
from src.core.utils import watched_item_event_base_metadata


class _FakeWatchedItem:
    """Minimal stand-in matching the attributes watched_item_event_base_metadata reads."""

    def __init__(self):
        self.domain_name = "example.com"
        self.default_schedule_config = {"interval": "1h"}
        self.last_changed_at = datetime(2026, 4, 15, 3, 22, 0, tzinfo=UTC)
        self.default_tags = ["regulatory"]
        self.description = "desc"


def _real_change_detected_keys() -> set[str]:
    """The metadata keys a real change_detected event actually carries.

    Base (watched_item_event_base_metadata) + the keys pipeline.py layers on
    for a detected change. Mirrors src/workers/pipeline.py's change_meta.
    """
    base = set(watched_item_event_base_metadata(_FakeWatchedItem()).keys())
    return base | {"change_revision_id", "content_fingerprint", "archiver_revision_id"}


class TestMockEventFixtures:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in MOCK_EVENT_FIXTURES, f"MOCK_EVENT_FIXTURES missing {et.value}"

    def test_change_detected_matches_pipeline_metadata(self):
        """#221 fidelity invariant: the fixture must not advertise keys a real
        change email never carries. Its keys must be a subset of what
        pipeline.py actually emits."""
        fx_keys = set(MOCK_EVENT_FIXTURES["change_detected"].keys())
        real_keys = _real_change_detected_keys()
        assert fx_keys <= real_keys, f"fixture has phantom keys: {fx_keys - real_keys}"
        # And the change-identity key is present so change_url renders.
        assert "change_revision_id" in fx_keys

    def test_change_detected_has_no_diff_phantom_keys(self):
        """The removed diff/significance keys must not reappear (regression guard)."""
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        for phantom in ("added", "modified", "removed", "significance", "change_id"):
            assert phantom not in fx

    def test_watch_error_has_status_code(self):
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert "status_code" in fx


class TestBuildPreviewEvent:
    def test_returns_watchevent_for_every_event_type(self):
        for et in WatchEventType:
            ev = build_preview_event(et.value)
            assert isinstance(ev, WatchEvent)
            assert ev.event_type == et

    def test_event_has_item_name_and_url(self):
        ev = build_preview_event("change_detected")
        assert ev.item_name
        assert ev.item_url.startswith("http")

    def test_event_has_occurred_at(self):
        ev = build_preview_event("change_detected")
        assert isinstance(ev.occurred_at, datetime)

    def test_metadata_includes_fixture_fields(self):
        ev = build_preview_event("change_detected")
        assert "domain_name" in ev.metadata
        assert "change_revision_id" in ev.metadata

    def test_unknown_event_type_raises(self):
        with pytest.raises(KeyError):
            build_preview_event("not_a_real_event_type")
