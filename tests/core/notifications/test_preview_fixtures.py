"""Tests for the notification preview mock-event fixtures."""

import hashlib
import re
from datetime import datetime

import pytest

from src.api.schemas.content_config import ContentOptions
from src.core.notifications.content import build_body
from src.core.notifications.diff import compute_change_diff
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.preview_fixtures import (
    MOCK_EVENT_FIXTURES,
    PREVIEW_CURRENT_TEXT,
    PREVIEW_PREVIOUS_TEXT,
    build_preview_event,
    preview_diff,
)
from src.core.utils import format_utc_iso


class TestMockEventFixtures:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in MOCK_EVENT_FIXTURES, f"MOCK_EVENT_FIXTURES missing {et.value}"

    def test_no_fixture_for_a_non_event(self):
        """#166: a fixture for a value that cannot fire lets the preview vouch for it."""
        assert set(MOCK_EVENT_FIXTURES) == {et.value for et in WatchEventType}

    def test_change_detected_has_no_phantom_keys(self):
        """The retired chunk/significance keys stay retired (#222 D8), and the
        diff itself is computed at dispatch, never carried as metadata."""
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        for phantom in (
            "added",
            "modified",
            "removed",
            "significance",
            "change_id",
            "unified_diff",
        ):
            assert phantom not in fx

    def test_fixture_fingerprints_address_the_canned_texts(self):
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        prev = hashlib.sha256(PREVIEW_PREVIOUS_TEXT).hexdigest()
        curr = hashlib.sha256(PREVIEW_CURRENT_TEXT).hexdigest()
        assert fx["previous_fingerprint"] == f"sha256:{prev}"
        assert fx["current_fingerprint"] == f"sha256:{curr}"

    def test_change_detected_dates_are_a_real_changes_dates(self):
        """CR 4: on a real change ``last_changed_at`` *is* this change (the
        pipeline sets it before building the event), and
        ``previous_changed_at`` is earlier."""
        event = build_preview_event("change_detected")
        assert event.metadata["last_changed_at"] == format_utc_iso(event.occurred_at)
        assert event.metadata["previous_changed_at"] < event.metadata["last_changed_at"]

    def test_watch_error_has_status_code(self):
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert "status_code" in fx

    def test_watch_error_previews_the_first_notification(self):
        """The preview shows a first alert, so a template using #71's repeat keys
        previews that form. Nothing emits them since F3 (#376); #379 deletes
        the preview with the rest of the subsystem."""
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert fx["renotify"] is False
        assert fx["previously_notified_at"] == ""

    def test_an_unguarded_repeat_variable_previews_as_it_dispatches(self):
        """CR 8: the strict preview must not reject a template that dispatches
        fine — the variable is documented as empty on a first alert."""
        event = build_preview_event("watch_error")
        options = ContentOptions(body_template="last told: {{ previously_notified_at }}")
        assert build_body(event, options, strict=True) == "last told: "


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


class TestPreviewDiff:
    def test_change_detected_diffs_the_canned_texts(self):
        assert preview_diff("change_detected") == compute_change_diff(
            PREVIEW_PREVIOUS_TEXT, PREVIEW_CURRENT_TEXT
        )

    def test_the_canned_texts_hold_an_unpunctuated_list_run(self):
        """#349: the common live shape — a schedule or list page with no
        sentence end — is the one #222's segmentation realigned on. The preview
        carries one, so a regression shows in the preview too."""
        run = max(PREVIEW_PREVIOUS_TEXT.split(b"\n"), key=len)
        assert len(run) > 800
        assert not re.search(rb"[.!?]\s", run)

    def test_an_early_insertion_in_the_run_marks_none_of_its_tail(self):
        """Everything after April is the run's tail: none of it is a change."""
        changed = [
            line
            for hunk in preview_diff("change_detected").hunks
            for line in hunk
            if line[:1] in "+-"
        ]
        assert "+ Recording: April 6 hearing video" in changed
        assert not any("May" in line for line in changed)

    def test_other_events_have_none(self):
        assert preview_diff("watch_error") is None
