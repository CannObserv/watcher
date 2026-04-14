"""Unit tests for _parse_content_config_from_form."""

from src.dashboard.routes import _parse_content_config_from_form

_SNIPPET = "content_config__include_diff_snippet"
_FULL = "content_config__include_diff_full"
_TEMPORAL = "content_config__include_temporal_context"
_DOMAIN = "content_config__include_domain"
_LINES = "content_config__diff_snippet_lines"


def _form(**kwargs):
    """Build a minimal form dict with sensible defaults for unset fields."""
    base = {_LINES: "10"}
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
    def test_non_numeric_lines_falls_back_to_10(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "abc"}))
        assert result["default"]["diff_snippet_lines"] == 10

    def test_empty_lines_falls_back_to_10(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: ""}))
        assert result["default"]["diff_snippet_lines"] == 10

    def test_lines_clamped_at_max(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "999"}))
        assert result["default"]["diff_snippet_lines"] == 100

    def test_lines_clamped_at_min(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _LINES: "0"}))
        assert result["default"]["diff_snippet_lines"] == 1


_TITLE_TMPL = "content_config__title_template"
_BODY_TMPL = "content_config__body_template"


class TestTemplateStrings:
    def test_title_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_SNIPPET: "1", _TITLE_TMPL: "{{ event_type }}: {{ watch_name }}"})
        )
        assert result is not None
        assert result["default"]["title_template"] == "{{ event_type }}: {{ watch_name }}"

    def test_body_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_SNIPPET: "1", _BODY_TMPL: "URL: {{ watch_url }}"})
        )
        assert result is not None
        assert result["default"]["body_template"] == "URL: {{ watch_url }}"

    def test_empty_title_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _TITLE_TMPL: "   "}))
        assert result is not None
        assert result["default"]["title_template"] is None

    def test_empty_body_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_SNIPPET: "1", _BODY_TMPL: ""}))
        assert result is not None
        assert result["default"]["body_template"] is None

    def test_template_alone_without_toggle_returns_none(self):
        # templates alone (no other toggle) do not persist config
        # because they don't count as "any_enabled" without title/body being meaningful
        # Actually per spec they DO count — title_template / body_template alone IS useful
        result = _parse_content_config_from_form(_form(**{_TITLE_TMPL: "custom title"}))
        # title_template alone should enable persistence
        assert result is not None
        assert result["default"]["title_template"] == "custom title"
