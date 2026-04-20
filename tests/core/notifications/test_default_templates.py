"""Tests for the default notification template registry."""

from src.api.schemas.content_config import ContentOptions
from src.core.notifications.default_templates import (
    ADDITIVE_BODY_SNIPPETS,
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


class TestAdditiveBodySnippets:
    def test_keys_match_contentoptions_toggle_fields(self):
        """Every `include_<name>` field on ContentOptions should have a snippet,
        and every snippet key should correspond to a toggle field."""
        toggle_names = {
            name.removeprefix("include_")
            for name in ContentOptions.model_fields
            if name.startswith("include_")
        }
        assert set(ADDITIVE_BODY_SNIPPETS.keys()) == toggle_names

    def test_all_snippets_are_nonempty_strings(self):
        for key, snippet in ADDITIVE_BODY_SNIPPETS.items():
            assert isinstance(snippet, str), f"{key} snippet not a string"
            assert snippet, f"{key} snippet empty"


class TestComposeTitlePrefill:
    def test_returns_default_title_template(self):
        result = compose_title_prefill("change_detected")
        assert result == DEFAULT_TITLE_TEMPLATES["change_detected"]


class TestComposeBodyPrefill:
    def test_default_only_returns_event_default_body(self):
        result = compose_body_prefill("change_detected", ContentOptions())
        assert result == DEFAULT_BODY_TEMPLATES["change_detected"]

    def test_appends_snippet_for_each_enabled_toggle(self):
        opts = ContentOptions(include_domain=True, include_significance=True)
        result = compose_body_prefill("change_detected", opts)
        # starts with event default body, then blank-line-separated snippets
        assert result.startswith(DEFAULT_BODY_TEMPLATES["change_detected"])
        assert ADDITIVE_BODY_SNIPPETS["domain"] in result
        assert ADDITIVE_BODY_SNIPPETS["significance"] in result

    def test_snippets_separated_by_blank_line(self):
        opts = ContentOptions(include_domain=True)
        result = compose_body_prefill("change_detected", opts)
        assert "\n\n" in result

    def test_no_toggles_no_snippets_in_output(self):
        result = compose_body_prefill("change_detected", ContentOptions())
        for snippet in ADDITIVE_BODY_SNIPPETS.values():
            assert snippet not in result


class TestTemplateVariables:
    def test_every_variable_is_a_template_variable_dataclass(self):
        for v in TEMPLATE_VARIABLES:
            assert isinstance(v, TemplateVariable)

    def test_core_variables_present(self):
        names = {v.name for v in TEMPLATE_VARIABLES}
        for required in ("watch_id", "watch_name", "watch_url", "event_type", "event_label"):
            assert required in names

    def test_scopes_are_valid(self):
        allowed = {"always", "change_detected", "watch_error", "contextual"}
        for v in TEMPLATE_VARIABLES:
            assert v.scope in allowed, f"{v.name} has invalid scope {v.scope}"

    def test_no_duplicate_variable_names(self):
        names = [v.name for v in TEMPLATE_VARIABLES]
        assert len(names) == len(set(names))
