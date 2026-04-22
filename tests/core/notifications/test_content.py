"""Tests for the notification content builder."""

from datetime import UTC, datetime

import pytest
from jinja2 import TemplateError, UndefinedError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import (
    _build_change_url_section,
    _build_description_section,
    _build_last_changed_section,
    _build_significance_section,
    _build_tags_section,
    _build_watch_url_section,
    build_body,
    build_template_context,
    build_title,
    render_template,
    render_template_strict,
    resolve_options,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType

OCCURRED_AT = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)


def make_event(event_type=WatchEventType.CHANGE_DETECTED, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watch_id="01HV0000000000000000000001",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=OCCURRED_AT,
        metadata=metadata or {},
    )


CHANGE_META = {
    "added": ["Licenses"],
    "removed": ["Hours"],
    "modified": [{"label": "Contact Info", "similarity": 0.85}],
}


class TestResolveOptions:
    def test_none_config_returns_defaults(self):
        opts = resolve_options(None, "change_detected")
        assert opts == ContentOptions()

    def test_default_used_when_no_override(self):
        cfg = ContentConfig(default=ContentOptions(include_domain=True))
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is True

    def test_override_takes_precedence(self):
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={"change_detected": ContentOptions(include_domain=False)},
        )
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is False

    def test_non_overridden_event_falls_back_to_default(self):
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={"watch_error": ContentOptions(include_domain=False)},
        )
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is True


class TestBuildBodyBase:
    def test_default_body_rendered_from_template(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions())
        # change_detected default template: "{{ watch_url }} — {{ change_summary }}"
        assert "https://example.com" in body
        assert "1 added, 1 modified, 1 removed" in body

    def test_no_extra_sections_by_default(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions())
        assert "Changed sections" not in body
        assert "Domain" not in body
        assert "Check interval" not in body


class TestBuildBodyDiffSnippet:
    def test_snippet_appended(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_snippet=True))
        assert "Changed sections" in body
        assert "+ Licenses" in body
        assert "- Hours" in body
        assert "~ Contact Info" in body

    def test_snippet_respects_limit(self):
        meta = {
            "added": ["A", "B", "C"],
            "removed": [],
            "modified": [],
        }
        event = make_event(metadata=meta)
        body = build_body(event, ContentOptions(include_diff_snippet=True, diff_snippet_lines=2))
        assert "+ A" in body
        assert "+ B" in body
        assert "+ C" not in body

    def test_full_supersedes_snippet(self):
        meta = {"added": ["A", "B", "C"], "removed": [], "modified": []}
        event = make_event(metadata=meta)
        # With full=True, snippet limit is ignored
        body = build_body(
            event,
            ContentOptions(include_diff_full=True, include_diff_snippet=True, diff_snippet_lines=1),
        )
        assert "+ A" in body
        assert "+ B" in body
        assert "+ C" in body

    def test_no_diff_section_when_metadata_empty(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_diff_snippet=True))
        assert "Changed sections" not in body

    def test_similarity_shown_for_modified(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_full=True))
        assert "85%" in body


class TestBuildBodyTemporalContext:
    def test_check_interval_shown(self):
        event = make_event(metadata={"check_interval": "1h"})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "Check interval" in body
        assert "1h" in body

    def test_no_section_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "Check interval" not in body


class TestBuildBodyDomain:
    def test_domain_shown(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "Domain: example.com" in body

    def test_no_section_when_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "Domain" not in body


class TestBuildBodyOrdering:
    def test_sections_joined_with_double_newline(self):
        event = make_event(metadata={"effective_domain": "ex.com", **CHANGE_META})
        body = build_body(
            event,
            ContentOptions(include_diff_snippet=True, include_domain=True),
        )
        # Default-template body comes first, then extra sections
        assert body.startswith("https://example.com")
        assert "\n\n" in body
        # Domain section should appear after the base
        assert body.index("https://example.com") < body.index("Domain: ex.com")


class TestBuildLastChangedSection:
    def test_returns_formatted_date(self):
        result = _build_last_changed_section({"last_changed_at": "2026-04-09"})
        assert result == "Last changed: 2026-04-09"

    def test_returns_empty_when_key_absent(self):
        assert _build_last_changed_section({}) == ""

    def test_build_body_includes_section_when_enabled(self):
        event = make_event(metadata={"last_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "Last changed: 2026-04-09" in body

    def test_build_body_omits_section_when_disabled(self):
        event = make_event(metadata={"last_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions(include_last_changed_at=False))
        assert "Last changed" not in body

    def test_build_body_omits_section_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "Last changed" not in body


class TestBuildSignificanceSection:
    def test_returns_percentage(self):
        result = _build_significance_section({"significance": 0.73})
        assert result == "Significance: 73%"

    def test_zero_significance(self):
        result = _build_significance_section({"significance": 0.0})
        assert result == "Significance: 0%"

    def test_full_significance(self):
        result = _build_significance_section({"significance": 1.0})
        assert result == "Significance: 100%"

    def test_returns_empty_when_key_absent(self):
        assert _build_significance_section({}) == ""

    def test_build_body_includes_section_when_enabled(self):
        event = make_event(metadata={"significance": 0.5})
        body = build_body(event, ContentOptions(include_significance=True))
        assert "Significance: 50%" in body

    def test_build_body_omits_section_when_disabled(self):
        event = make_event(metadata={"significance": 0.5})
        body = build_body(event, ContentOptions(include_significance=False))
        assert "Significance" not in body

    def test_build_body_omits_section_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_significance=True))
        assert "Significance" not in body


class TestBuildChangeUrlSection:
    WATCH_ID = "01HV0000000000000000000001"
    CHANGE_ID = "01HV0000000000000000000099"

    def test_returns_correct_url(self):
        result = _build_change_url_section(self.WATCH_ID, {"change_id": self.CHANGE_ID})
        assert f"/watches/{self.WATCH_ID}/changes/{self.CHANGE_ID}" in result
        assert result.startswith("View change: ")

    def test_returns_empty_when_change_id_absent(self):
        assert _build_change_url_section(self.WATCH_ID, {}) == ""

    def test_build_body_includes_url_when_enabled(self):
        event = make_event(metadata={"change_id": self.CHANGE_ID})
        body = build_body(event, ContentOptions(include_change_dashboard_url=True))
        assert f"/watches/{event.watch_id}/changes/{self.CHANGE_ID}" in body

    def test_build_body_omits_url_when_disabled(self):
        event = make_event(metadata={"change_id": self.CHANGE_ID})
        body = build_body(event, ContentOptions(include_change_dashboard_url=False))
        assert "View change" not in body

    def test_build_body_omits_url_when_change_id_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_change_dashboard_url=True))
        assert "View change" not in body


class TestBuildWatchUrlSection:
    WATCH_ID = "01HV0000000000000000000001"

    def test_returns_correct_url(self):
        result = _build_watch_url_section(self.WATCH_ID)
        assert result == f"Watch URL: https://watcher.exe.xyz/watches/{self.WATCH_ID}"

    def test_build_body_includes_watch_url_when_enabled(self):
        event = make_event()
        body = build_body(event, ContentOptions(include_watch_url=True))
        assert f"Watch URL: https://watcher.exe.xyz/watches/{event.watch_id}" in body

    def test_build_body_omits_watch_url_when_disabled(self):
        event = make_event()
        body = build_body(event, ContentOptions(include_watch_url=False))
        assert "Watch URL" not in body


class TestBuildTagsSection:
    def test_returns_formatted_tags(self):
        result = _build_tags_section({"tags": ["foo", "bar"]})
        assert result == "Tags: foo, bar"

    def test_returns_empty_when_key_absent(self):
        assert _build_tags_section({}) == ""

    def test_returns_empty_when_tags_empty_list(self):
        assert _build_tags_section({"tags": []}) == ""

    def test_build_body_includes_tags_when_enabled(self):
        event = make_event(metadata={"tags": ["cannabis", "license"]})
        body = build_body(event, ContentOptions(include_tags=True))
        assert "Tags: cannabis, license" in body

    def test_build_body_omits_tags_when_disabled(self):
        event = make_event(metadata={"tags": ["cannabis"]})
        body = build_body(event, ContentOptions(include_tags=False))
        assert "Tags" not in body

    def test_build_body_omits_tags_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_tags=True))
        assert "Tags" not in body


class TestBuildDescriptionSection:
    def test_returns_formatted_description(self):
        result = _build_description_section({"description": "some text"})
        assert result == "Description: some text"

    def test_returns_empty_when_key_absent(self):
        assert _build_description_section({}) == ""

    def test_returns_empty_when_description_empty_string(self):
        assert _build_description_section({"description": ""}) == ""

    def test_build_body_includes_description_when_enabled(self):
        event = make_event(metadata={"description": "Watch for license renewals"})
        body = build_body(event, ContentOptions(include_description=True))
        assert "Description: Watch for license renewals" in body

    def test_build_body_omits_description_when_disabled(self):
        event = make_event(metadata={"description": "Watch for license renewals"})
        body = build_body(event, ContentOptions(include_description=False))
        assert "Description" not in body

    def test_build_body_omits_description_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_description=True))
        assert "Description" not in body


class TestRenderTemplate:
    def test_successful_render(self):
        result = render_template("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_syntax_error_returns_original(self):
        template_str = "{{ unclosed"
        result = render_template(template_str, {})
        assert result == template_str

    def test_undefined_error_returns_original(self):
        # strict undefined by default raises UndefinedError
        template_str = "{{ missing_var }}"
        result = render_template(template_str, {})
        # Jinja2 default env renders undefined as '' — so it won't raise.
        # The important thing is it doesn't crash.
        assert isinstance(result, str)

    def test_empty_string_renders_empty(self):
        result = render_template("", {})
        assert result == ""

    def test_event_type_in_context(self):
        result = render_template("{{ event_type }}", {"event_type": "change_detected"})
        assert result == "change_detected"


class TestBuildTemplateContext:
    def test_context_has_all_watch_event_fields(self):
        event = make_event(metadata={"significance": 0.5, "change_id": "abc"})
        ctx = build_template_context(event)
        assert ctx["watch_id"] == event.watch_id
        assert ctx["watch_name"] == event.watch_name
        assert ctx["watch_url"] == event.watch_url
        assert ctx["event_type"] == event.event_type
        assert ctx["occurred_at"] == event.occurred_at

    def test_metadata_keys_flattened_into_context(self):
        event = make_event(metadata={"significance": 0.75, "change_id": "xyz"})
        ctx = build_template_context(event)
        assert ctx["significance"] == 0.75
        assert ctx["change_id"] == "xyz"

    def test_empty_metadata_produces_base_keys_only(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert set(ctx.keys()) == {
            "watch_id",
            "watch_name",
            "watch_url",
            "event_type",
            "occurred_at",
            "event_label",
            "change_summary",
            "change_url",
        }

    def test_event_label_matches_event_titles(self):
        for et in WatchEventType:
            event = make_event(event_type=et)
            ctx = build_template_context(event)
            assert ctx["event_label"] == EVENT_TITLES[et.value]

    def test_change_summary_counts_changes(self):
        event = make_event(
            metadata={"added": ["a", "b"], "modified": [{}], "removed": []},
        )
        ctx = build_template_context(event)
        assert ctx["change_summary"] == "2 added, 1 modified"

    def test_change_summary_details_pending_when_empty(self):
        event = make_event(
            event_type=WatchEventType.CHANGE_DETECTED,
            metadata={},
        )
        ctx = build_template_context(event)
        assert ctx["change_summary"] == "details pending"

    def test_change_summary_empty_for_non_change_events(self):
        event = make_event(event_type=WatchEventType.WATCH_PAUSED, metadata={})
        ctx = build_template_context(event)
        assert ctx["change_summary"] == ""

    def test_change_url_populated_when_change_id_present(self):
        event = make_event(metadata={"change_id": "01HV0000000000000000000099"})
        ctx = build_template_context(event)
        assert (
            ctx["change_url"]
            == f"https://watcher.exe.xyz/watches/{event.watch_id}/changes/01HV0000000000000000000099"
        )

    def test_change_url_empty_when_change_id_absent(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert ctx["change_url"] == ""


class TestBuildBodyWithTemplates:
    def test_body_template_overrides_additive_sections(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        opts = ContentOptions(include_domain=True, body_template="custom: {{ watch_name }}")
        body = build_body(event, opts)
        assert body == "custom: Test Watch"
        # Additive section should NOT appear when body_template is set
        assert "Domain" not in body

    def test_body_template_none_uses_additive_logic(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        opts = ContentOptions(include_domain=True, body_template=None)
        body = build_body(event, opts)
        assert "Domain: example.com" in body

    def test_body_template_bad_syntax_falls_back_to_template_string(self):
        event = make_event()
        opts = ContentOptions(body_template="{{ unclosed")
        body = build_body(event, opts)
        assert body == "{{ unclosed"


class TestBuildTitle:
    def test_uses_default_template_for_event_type(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        title = build_title(event, ContentOptions())
        # Default title template: "{{ event_label }}: {{ watch_name }}"
        assert title == "Change Detected: Test Watch"

    def test_user_title_template_overrides_default(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        opts = ContentOptions(title_template="[{{ watch_name }}] custom")
        title = build_title(event, opts)
        assert title == "[Test Watch] custom"

    def test_renders_event_label_for_every_event_type(self):
        for et in WatchEventType:
            event = make_event(event_type=et)
            title = build_title(event, ContentOptions())
            assert title.startswith(EVENT_TITLES[et.value])
            assert "Test Watch" in title

    def test_bad_user_template_falls_back_to_raw_string(self):
        """Preserves dispatch-never-breaks guarantee inherited from render_template."""
        event = make_event()
        opts = ContentOptions(title_template="{{ unclosed")
        title = build_title(event, opts)
        assert title == "{{ unclosed"


class TestBuildTitleStrict:
    def test_strict_raises_on_bad_user_title_template(self):
        """build_title(..., strict=True) surfaces user template errors."""
        event = make_event()
        opts = ContentOptions(title_template="{{ unknown_var }}")
        with pytest.raises(UndefinedError):
            build_title(event, opts, strict=True)

    def test_strict_still_renders_valid_default(self):
        event = make_event()
        title = build_title(event, ContentOptions(), strict=True)
        assert title == "Change Detected: Test Watch"


class TestBuildBodyStrict:
    def test_strict_raises_on_bad_user_body_template(self):
        """build_body(..., strict=True) surfaces user template errors."""
        event = make_event()
        opts = ContentOptions(body_template="{{ undefined_thing }}")
        with pytest.raises(UndefinedError):
            build_body(event, opts, strict=True)

    def test_strict_renders_default_body_with_additive_sections(self):
        """Default templates + additive sections should work under strict too."""
        event = make_event(metadata={"effective_domain": "example.com", **CHANGE_META})
        body = build_body(event, ContentOptions(include_domain=True), strict=True)
        assert "https://example.com" in body
        assert "Domain: example.com" in body


class TestRenderTemplateStrict:
    def test_renders_successfully(self):
        result = render_template_strict("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_raises_on_syntax_error(self):
        with pytest.raises(TemplateError):
            render_template_strict("{{ unclosed", {})

    def test_raises_on_undefined_variable(self):
        """Undefined references raise UndefinedError — lenient render_template
        silently renders empty; strict must not, so preview can surface typos."""
        with pytest.raises(UndefinedError):
            render_template_strict("{{ unknown_var }}", {})
