"""Unit tests for _parse_content_config_from_form."""

from src.dashboard.routes import _parse_content_config_from_form

_SNIPPET = "content_config__include_diff_snippet"
_FULL = "content_config__include_diff_full"
_TEMPORAL = "content_config__include_temporal_context"
_DOMAIN = "content_config__include_domain"
_LINES = "content_config__diff_snippet_lines"


def _form(**kwargs):
    """Build a minimal form dict with sensible defaults for unset fields."""
    base = {_LINES: "25"}
    base.update(kwargs)
    return base


class TestNoTogglesEnabled:
    def test_all_defaults_returns_none(self):
        assert _parse_content_config_from_form(_form()) is None

    def test_nondefault_lines_only_returns_none(self):
        # diff_snippet_lines alone (no toggle) should not persist a config
        assert _parse_content_config_from_form(_form(**{_LINES: "5"})) is None

    def test_empty_form_returns_none(self):
        assert _parse_content_config_from_form({}) is None


class TestTogglesEnabled:
    def test_snippet_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1"}))
        assert result is not None
        assert result["default"]["include_diff_snippet"] is True

    def test_full_diff_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_FULL: "1"}))
        assert result is not None
        assert result["default"]["include_diff_full"] is True

    def test_temporal_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_TEMPORAL: "1"}))
        assert result is not None
        assert result["default"]["include_temporal_context"] is True

    def test_domain_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_DOMAIN: "1"}))
        assert result is not None
        assert result["default"]["include_domain"] is True

    def test_lines_preserved_when_snippet_enabled(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "25"}))
        assert result["default"]["diff_snippet_lines"] == 25


class TestLinesGuard:
    def test_non_numeric_lines_falls_back_to_default(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "abc"}))
        assert result["default"]["diff_snippet_lines"] == 25

    def test_empty_lines_falls_back_to_default(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: ""}))
        assert result["default"]["diff_snippet_lines"] == 25

    def test_lines_clamped_at_max(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "999"}))
        assert result["default"]["diff_snippet_lines"] == 200

    def test_lines_clamped_at_min(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "0"}))
        assert result["default"]["diff_snippet_lines"] == 1


_TITLE_TMPL = "content_config__title_template"
_BODY_TMPL = "content_config__body_template"


class TestTemplateStrings:
    def test_title_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_SNIPPET: "1", _TITLE_TMPL: "{{ event_type }}: {{ item_name }}"})
        )
        assert result is not None
        assert result["default"]["title_template"] == "{{ event_type }}: {{ item_name }}"

    def test_body_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_SNIPPET: "1", _BODY_TMPL: "URL: {{ item_url }}"})
        )
        assert result is not None
        assert result["default"]["body_template"] == "URL: {{ item_url }}"

    def test_empty_title_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _TITLE_TMPL: "   "}))
        assert result is not None
        assert result["default"]["title_template"] is None

    def test_empty_body_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _BODY_TMPL: ""}))
        assert result is not None
        assert result["default"]["body_template"] is None

    def test_title_template_alone_persists_config(self):
        # title_template alone (no boolean toggles) is enough to persist config.
        result = _parse_content_config_from_form(_form(**{_TITLE_TMPL: "custom title"}))
        assert result is not None
        assert result["default"]["title_template"] == "custom title"


class TestPerEventOverrides:
    def test_per_event_override_round_trip(self):
        form = _form(
            **{
                _SNIPPET: "1",
                "content_config__override__change_detected__include_diff_snippet": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        assert result is not None
        assert "change_detected" in result["overrides"]
        assert result["overrides"]["change_detected"]["include_diff_snippet"] is True

    def test_multiple_overrides_parsed(self):
        form = _form(
            **{
                _SNIPPET: "1",
                "content_config__override__change_detected__include_diff_snippet": "1",
                "content_config__override__watch_error__include_domain": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        assert result is not None
        assert "change_detected" in result["overrides"]
        assert "watch_error" in result["overrides"]
        assert result["overrides"]["watch_error"]["include_domain"] is True

    def test_no_overrides_when_no_per_event_toggles(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1"}))
        assert result is not None
        assert result["overrides"] == {}

    def test_event_type_not_added_when_no_toggles_checked(self):
        # All override keys unchecked for change_detected → not in overrides
        form = _form(**{_SNIPPET: "1"})
        result = _parse_content_config_from_form(form)
        assert "change_detected" not in result["overrides"]

    def test_override_all_fields_parsed(self):
        et = "watch_recovered"
        form = _form(
            **{
                _SNIPPET: "1",
                f"content_config__override__{et}__include_diff_snippet": "1",
                f"content_config__override__{et}__include_diff_full": "1",
                f"content_config__override__{et}__include_temporal_context": "1",
                f"content_config__override__{et}__include_domain": "1",
                f"content_config__override__{et}__include_last_changed_at": "1",
                f"content_config__override__{et}__include_significance": "1",
                f"content_config__override__{et}__include_change_dashboard_url": "1",
                f"content_config__override__{et}__include_tags": "1",
                f"content_config__override__{et}__include_description": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        ov = result["overrides"][et]
        assert ov["include_diff_snippet"] is True
        assert ov["include_diff_full"] is True
        assert ov["include_temporal_context"] is True
        assert ov["include_domain"] is True
        assert ov["include_last_changed_at"] is True
        assert ov["include_significance"] is True
        assert ov["include_change_dashboard_url"] is True
        assert ov["include_tags"] is True
        assert ov["include_description"] is True
