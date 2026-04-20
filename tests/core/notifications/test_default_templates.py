"""Tests for the default notification template registry."""

from src.core.notifications.default_templates import (
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.events import WatchEventType


class TestDefaultTitleTemplates:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in DEFAULT_TITLE_TEMPLATES, (
                f"DEFAULT_TITLE_TEMPLATES missing entry for {et.value}"
            )

    def test_values_are_jinja_strings(self):
        for value in DEFAULT_TITLE_TEMPLATES.values():
            assert isinstance(value, str)
            assert "{{" in value  # at least one variable

    def test_references_event_label_and_watch_name(self):
        # Every default title should reference the event label and watch name —
        # this is the minimum useful title across all event types.
        for et, tmpl in DEFAULT_TITLE_TEMPLATES.items():
            assert "event_label" in tmpl, f"{et} default title missing event_label"
            assert "watch_name" in tmpl, f"{et} default title missing watch_name"


class TestDefaultBodyTemplates:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in DEFAULT_BODY_TEMPLATES, (
                f"DEFAULT_BODY_TEMPLATES missing entry for {et.value}"
            )

    def test_values_are_jinja_strings(self):
        for value in DEFAULT_BODY_TEMPLATES.values():
            assert isinstance(value, str)
            assert value  # non-empty

    def test_change_detected_references_change_summary(self):
        tmpl = DEFAULT_BODY_TEMPLATES["change_detected"]
        assert "change_summary" in tmpl
        assert "watch_url" in tmpl

    def test_watch_error_references_status_code(self):
        tmpl = DEFAULT_BODY_TEMPLATES["watch_error"]
        assert "status_code" in tmpl
        assert "watch_url" in tmpl
