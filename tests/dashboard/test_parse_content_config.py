"""Unit tests for _parse_content_config_from_form.

#221: the diff/significance toggles were removed. Parsing now covers the
surviving Context toggles (domain, temporal_context, last_changed_at, tags,
description) plus title/body templates and per-event overrides.
"""

from src.dashboard.routes import _parse_content_config_from_form

_DOMAIN = "content_config__include_domain"
_TEMPORAL = "content_config__include_temporal_context"
_LAST_CHANGED = "content_config__include_last_changed_at"
_TAGS = "content_config__include_tags"
_DESCRIPTION = "content_config__include_description"


def _form(**kwargs):
    """Build a form dict from the given fields (no implicit defaults)."""
    return dict(kwargs)


class TestNoTogglesEnabled:
    def test_all_defaults_returns_none(self):
        assert _parse_content_config_from_form(_form()) is None

    def test_empty_form_returns_none(self):
        assert _parse_content_config_from_form({}) is None


class TestTogglesEnabled:
    def test_domain_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_DOMAIN: "1"}))
        assert result is not None
        assert result["default"]["include_domain"] is True

    def test_temporal_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_TEMPORAL: "1"}))
        assert result is not None
        assert result["default"]["include_temporal_context"] is True

    def test_last_changed_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_LAST_CHANGED: "1"}))
        assert result is not None
        assert result["default"]["include_last_changed_at"] is True

    def test_tags_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_TAGS: "1"}))
        assert result is not None
        assert result["default"]["include_tags"] is True

    def test_description_toggle_returns_config(self):
        result = _parse_content_config_from_form(_form(**{_DESCRIPTION: "1"}))
        assert result is not None
        assert result["default"]["include_description"] is True


_TITLE_TMPL = "content_config__title_template"
_BODY_TMPL = "content_config__body_template"


class TestTemplateStrings:
    def test_title_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_DOMAIN: "1", _TITLE_TMPL: "{{ event_type }}: {{ item_name }}"})
        )
        assert result is not None
        assert result["default"]["title_template"] == "{{ event_type }}: {{ item_name }}"

    def test_body_template_round_trip(self):
        result = _parse_content_config_from_form(
            _form(**{_DOMAIN: "1", _BODY_TMPL: "URL: {{ item_url }}"})
        )
        assert result is not None
        assert result["default"]["body_template"] == "URL: {{ item_url }}"

    def test_empty_title_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_DOMAIN: "1", _TITLE_TMPL: "   "}))
        assert result is not None
        assert result["default"]["title_template"] is None

    def test_empty_body_template_stored_as_none(self):
        result = _parse_content_config_from_form(_form(**{_DOMAIN: "1", _BODY_TMPL: ""}))
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
                _DOMAIN: "1",
                "content_config__override__change_detected__include_domain": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        assert result is not None
        assert "change_detected" in result["overrides"]
        assert result["overrides"]["change_detected"]["include_domain"] is True

    def test_multiple_overrides_parsed(self):
        form = _form(
            **{
                _DOMAIN: "1",
                "content_config__override__change_detected__include_domain": "1",
                "content_config__override__watch_error__include_tags": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        assert result is not None
        assert "change_detected" in result["overrides"]
        assert "watch_error" in result["overrides"]
        assert result["overrides"]["watch_error"]["include_tags"] is True

    def test_no_overrides_when_no_per_event_toggles(self):
        result = _parse_content_config_from_form(_form(**{_DOMAIN: "1"}))
        assert result is not None
        assert result["overrides"] == {}

    def test_event_type_not_added_when_no_toggles_checked(self):
        # All override keys unchecked for change_detected → not in overrides
        form = _form(**{_DOMAIN: "1"})
        result = _parse_content_config_from_form(form)
        assert "change_detected" not in result["overrides"]

    def test_override_all_fields_parsed(self):
        et = "watch_recovered"
        form = _form(
            **{
                _DOMAIN: "1",
                f"content_config__override__{et}__include_temporal_context": "1",
                f"content_config__override__{et}__include_domain": "1",
                f"content_config__override__{et}__include_last_changed_at": "1",
                f"content_config__override__{et}__include_tags": "1",
                f"content_config__override__{et}__include_description": "1",
            }
        )
        result = _parse_content_config_from_form(form)
        ov = result["overrides"][et]
        assert ov["include_temporal_context"] is True
        assert ov["include_domain"] is True
        assert ov["include_last_changed_at"] is True
        assert ov["include_tags"] is True
        assert ov["include_description"] is True
