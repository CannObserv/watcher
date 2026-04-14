"""Tests for the notification content builder."""

from datetime import UTC, datetime

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import (
    _build_change_url_section,
    _build_description_section,
    _build_last_changed_section,
    _build_significance_section,
    _build_tags_section,
    build_body,
    resolve_options,
)
from src.core.notifications.events import WatchEvent, WatchEventType

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
    def test_base_body_always_present(self):
        event = make_event()
        body = build_body(event, ContentOptions())
        assert event.body in body

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
        # Base body comes first, then extra sections
        assert body.startswith(event.body)
        assert "\n\n" in body


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
