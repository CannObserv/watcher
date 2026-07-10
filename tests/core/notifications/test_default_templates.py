"""Tests for the default notification template registry."""

from src.core.notifications.default_templates import (
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
    TEMPLATE_VARIABLES,
    TemplateVariable,
    compose_body_prefill,
    compose_title_prefill,
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

    def test_references_event_label_and_item_name(self):
        for et, tmpl in DEFAULT_TITLE_TEMPLATES.items():
            assert "event_label" in tmpl, f"{et} default title missing event_label"
            assert "item_name" in tmpl, f"{et} default title missing item_name"

    def test_all_titles_prefixed_watcher(self):
        """The [Watcher] prefix lets users filter watcher notifications by
        subject alongside sister services (notifier, archiver). Switched from
        [Observo] in #155."""
        for et, tmpl in DEFAULT_TITLE_TEMPLATES.items():
            assert tmpl.startswith("[Watcher] "), f"{et} missing [Watcher] prefix"


class TestDefaultBodyTemplates:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in DEFAULT_BODY_TEMPLATES, (
                f"DEFAULT_BODY_TEMPLATES missing entry for {et.value}"
            )

    def test_values_are_nonempty_strings(self):
        for value in DEFAULT_BODY_TEMPLATES.values():
            assert isinstance(value, str)
            assert value

    def test_change_detected_references_always_present_skeleton(self):
        """The change_detected default body is the always-present header
        skeleton. Toggle-driven sections are interleaved in Python by
        `build_body`, not in the Jinja template. The event_label/change_summary
        body block was retired in #221; the header link is now ITEM."""
        tmpl = DEFAULT_BODY_TEMPLATES["change_detected"]
        assert "{{ item_name }}" in tmpl
        assert "URL: {{ item_url }}" in tmpl
        assert "TIMESTAMP: {{ occurred_at_iso }}" in tmpl
        assert "ITEM: https://watcher.exe.xyz/watched-items/{{ watched_item_id }}" in tmpl
        # Retired in #221 — must not reappear.
        assert "change_summary" not in tmpl
        assert "WATCH:" not in tmpl

    def test_change_detected_has_no_include_conditionals(self):
        """Toggle-driven sections live in Python composition, not in the
        default template — keeps custom body_template users from tripping
        over undefined `include_*` variables in the seed."""
        tmpl = DEFAULT_BODY_TEMPLATES["change_detected"]
        assert "include_" not in tmpl

    def test_watch_error_references_status_code(self):
        tmpl = DEFAULT_BODY_TEMPLATES["watch_error"]
        assert "status_code" in tmpl
        assert "item_url" in tmpl


class TestComposeTitlePrefill:
    def test_returns_default_title_template(self):
        result = compose_title_prefill("change_detected")
        assert result == DEFAULT_TITLE_TEMPLATES["change_detected"]


class TestComposeBodyPrefill:
    def test_returns_default_body_for_event_type(self):
        """Seed button returns the default body skeleton verbatim — no
        toggle-driven composition."""
        result = compose_body_prefill("change_detected")
        assert result == DEFAULT_BODY_TEMPLATES["change_detected"]

    def test_returns_default_body_for_watch_error(self):
        result = compose_body_prefill("watch_error")
        assert result == DEFAULT_BODY_TEMPLATES["watch_error"]


class TestTemplateVariables:
    def test_every_variable_is_a_template_variable_dataclass(self):
        for v in TEMPLATE_VARIABLES:
            assert isinstance(v, TemplateVariable)

    def test_core_variables_present(self):
        names = {v.name for v in TEMPLATE_VARIABLES}
        for required in (
            "watched_item_id",
            "item_name",
            "item_url",
            "event_type",
            "event_label",
            "occurred_at_iso",
        ):
            assert required in names

    def test_change_url_is_change_detected_scoped(self):
        """change_url survives #221 as a change_detected-scoped variable for
        custom templates (the diff/chunk variables were removed)."""
        var = next((v for v in TEMPLATE_VARIABLES if v.name == "change_url"), None)
        assert var is not None, "TEMPLATE_VARIABLES missing change_url"
        assert var.scope == "change_detected"

    def test_removed_diff_variables_absent(self):
        """The diff/significance/summary variables were removed in #221."""
        names = {v.name for v in TEMPLATE_VARIABLES}
        for removed in (
            "change_summary",
            "added",
            "modified",
            "removed",
            "diff_snippet",
            "diff_full",
            "chunks_changed",
            "significance",
            "change_id",
        ):
            assert removed not in names

    def test_scopes_are_valid(self):
        allowed = {"always", "change_detected", "watch_error", "contextual"}
        for v in TEMPLATE_VARIABLES:
            assert v.scope in allowed, f"{v.name} has invalid scope {v.scope}"

    def test_no_duplicate_variable_names(self):
        names = [v.name for v in TEMPLATE_VARIABLES]
        assert len(names) == len(set(names))
