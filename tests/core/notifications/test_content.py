"""Tests for the notification content builder.

#221: the diff/significance/change_summary machinery was stripped (Phase 5
removed the diff pipeline that fed it; restoration tracked in #222). The
change_detected body is now the header skeleton plus the surviving Context
toggles (Domain, Last changed, Check interval, Description, Tags). The header
link is labelled ITEM (was WATCH).
"""

from datetime import UTC, datetime

import pytest
from jinja2 import TemplateError, UndefinedError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import (
    build_body,
    build_template_context,
    build_title,
    render_template,
    render_template_strict,
    resolve_options,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType

OCCURRED_AT = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)
WATCH_ID = "01HV0000000000000000000001"


def make_event(event_type=WatchEventType.CHANGE_DETECTED, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watched_item_id=WATCH_ID,
        item_name="Test Watch",
        item_url="https://example.com",
        occurred_at=OCCURRED_AT,
        metadata=metadata or {},
    )


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


class TestChangeDetectedDefaultBody:
    """The change_detected default body is composed in Python (build_body) by
    interleaving toggle-driven sections into the always-present header
    skeleton at the canonical layout positions."""

    def test_default_skeleton_with_no_toggles(self):
        """With every toggle off, the body is the header skeleton alone:
        item_name, URL, TIMESTAMP, ITEM. The old event_label/change_summary
        body block was retired in #221."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        expected = (
            "Test Watch\n"
            "URL: https://example.com\n"
            "TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"ITEM: https://watcher.exe.xyz/watched-items/{WATCH_ID}"
        )
        assert body == expected

    def test_item_link_unconditional(self):
        """The ITEM dashboard link is part of the always-present skeleton —
        there is no toggle to suppress it."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        assert f"ITEM: https://watcher.exe.xyz/watched-items/{WATCH_ID}" in body

    def test_full_layout_with_every_toggle_on(self):
        """With every surviving toggle on and full metadata: DOMAIN between
        item_name and URL; LAST CHANGED + INTERVAL between URL and TIMESTAMP;
        DESCRIPTION + TAGS as trailing paragraphs."""
        event = make_event(
            metadata={
                "change_revision_id": "01HV0000000000000000000099",
                "domain_name": "example.com",
                "check_interval": "1h",
                "last_changed_at": "2026-04-09",
                "description": "Watch for license renewals",
                "tags": ["cannabis", "license"],
            }
        )
        opts = ContentOptions(
            include_temporal_context=True,
            include_domain=True,
            include_last_changed_at=True,
            include_description=True,
            include_tags=True,
        )
        body = build_body(event, opts)
        expected = (
            "Test Watch\n"
            "DOMAIN: example.com\n"
            "URL: https://example.com\n"
            "LAST CHANGED: 2026-04-09\n"
            "INTERVAL: 1h\n"
            "TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"ITEM: https://watcher.exe.xyz/watched-items/{WATCH_ID}"
            "\n\n"
            "DESCRIPTION: Watch for license renewals"
            "\n\n"
            "TAGS: cannabis, license"
        )
        assert body == expected

    def test_minimal_metadata_renders_under_strict(self):
        """Strict mode (preview endpoint) must not raise on missing metadata.
        Pure-Python composition has no Jinja in the default-body path so
        StrictUndefined never sees the toggle-gated branches."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_domain=True), strict=True)
        assert "Test Watch" in body
        # Toggle is on but metadata absent → DOMAIN slot skipped.
        assert "DOMAIN" not in body

    def test_seed_template_matches_dispatcher_output_with_default_options(self):
        """Single-source-of-truth invariant for the change_detected skeleton:
        rendering DEFAULT_BODY_TEMPLATES['change_detected'] (the UI seed)
        with default options must equal what build_body produces at dispatch
        time. Catches drift between the seed shown to the user and the body
        actually delivered."""
        from src.core.notifications.default_templates import DEFAULT_BODY_TEMPLATES

        event = make_event(metadata={})  # no optional sections in either path
        seed_rendered = render_template(
            DEFAULT_BODY_TEMPLATES["change_detected"], build_template_context(event)
        )
        dispatch_output = build_body(event, ContentOptions())
        assert seed_rendered == dispatch_output


class TestDomainSlot:
    def test_renders_between_name_and_url_when_toggle_on_and_metadata_present(self):
        event = make_event(metadata={"domain_name": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True))
        # DOMAIN appears between item_name (line 0) and URL (line 2).
        lines = body.split("\n")
        assert lines[0] == "Test Watch"
        assert lines[1] == "DOMAIN: example.com"
        assert lines[2].startswith("URL:")

    def test_omitted_when_toggle_off(self):
        event = make_event(metadata={"domain_name": "example.com"})
        body = build_body(event, ContentOptions(include_domain=False))
        assert "DOMAIN" not in body

    def test_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "DOMAIN" not in body


class TestStatsSlots:
    def test_interval_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"check_interval": "1h"})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "INTERVAL: 1h" in body

    def test_last_changed_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"last_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "LAST CHANGED: 2026-04-09" in body

    def test_last_changed_and_interval_render_between_url_and_timestamp(self):
        """LAST CHANGED + INTERVAL sit between URL and TIMESTAMP in the header,
        with LAST CHANGED first."""
        event = make_event(metadata={"check_interval": "1h", "last_changed_at": "2026-04-09"})
        body = build_body(
            event,
            ContentOptions(include_temporal_context=True, include_last_changed_at=True),
        )
        assert (
            "URL: https://example.com\nLAST CHANGED: 2026-04-09\nINTERVAL: 1h\nTIMESTAMP: "
        ) in body

    def test_stats_omitted_when_toggles_off(self):
        event = make_event(metadata={"check_interval": "1h", "last_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions())
        assert "INTERVAL" not in body
        assert "LAST CHANGED" not in body

    def test_stats_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(
            event,
            ContentOptions(
                include_temporal_context=True,
                include_last_changed_at=True,
            ),
        )
        assert "INTERVAL" not in body
        assert "LAST CHANGED" not in body


class TestDescriptionSlot:
    def test_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"description": "Watch for license renewals"})
        body = build_body(event, ContentOptions(include_description=True))
        assert "DESCRIPTION: Watch for license renewals" in body

    def test_omitted_when_toggle_off(self):
        event = make_event(metadata={"description": "x"})
        body = build_body(event, ContentOptions(include_description=False))
        assert "DESCRIPTION" not in body

    def test_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_description=True))
        assert "DESCRIPTION" not in body

    def test_omitted_when_description_empty_string(self):
        event = make_event(metadata={"description": ""})
        body = build_body(event, ContentOptions(include_description=True))
        assert "DESCRIPTION" not in body


class TestTagsSlot:
    def test_renders_comma_joined_when_toggle_on(self):
        event = make_event(metadata={"tags": ["cannabis", "license"]})
        body = build_body(event, ContentOptions(include_tags=True))
        assert "TAGS: cannabis, license" in body

    def test_omitted_when_toggle_off(self):
        event = make_event(metadata={"tags": ["x"]})
        body = build_body(event, ContentOptions(include_tags=False))
        assert "TAGS" not in body

    def test_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_tags=True))
        assert "TAGS" not in body

    def test_omitted_when_tags_empty_list(self):
        event = make_event(metadata={"tags": []})
        body = build_body(event, ContentOptions(include_tags=True))
        assert "TAGS" not in body


class TestNonChangeDetectedDefaultBody:
    """Non-change_detected events render straight from DEFAULT_BODY_TEMPLATES.
    Toggles do not apply — the default body is a single Jinja line."""

    def test_watch_error_renders_default_template(self):
        event = make_event(event_type=WatchEventType.WATCH_ERROR, metadata={"status_code": 500})
        body = build_body(event, ContentOptions(include_domain=True))
        assert body == "https://example.com returned HTTP 500"

    def test_watch_paused_renders_default_template(self):
        event = make_event(event_type=WatchEventType.WATCH_PAUSED, metadata={})
        body = build_body(event, ContentOptions())
        assert body == "Watch paused: https://example.com"


class TestRenderTemplate:
    def test_successful_render(self):
        result = render_template("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_syntax_error_returns_original(self):
        template_str = "{{ unclosed"
        result = render_template(template_str, {})
        assert result == template_str

    def test_undefined_renders_empty_in_lenient_mode(self):
        # Default Jinja env renders undefined as '' — never raises.
        result = render_template("{{ missing_var }}", {})
        assert isinstance(result, str)

    def test_empty_string_renders_empty(self):
        result = render_template("", {})
        assert result == ""


class TestBuildTemplateContext:
    def test_context_has_all_watch_event_fields(self):
        event = make_event(metadata={"change_revision_id": "abc"})
        ctx = build_template_context(event)
        assert ctx["watched_item_id"] == event.watched_item_id
        assert ctx["item_name"] == event.item_name
        assert ctx["item_url"] == event.item_url
        assert ctx["event_type"] == event.event_type
        assert ctx["occurred_at"] == event.occurred_at

    def test_metadata_keys_flattened_into_context(self):
        event = make_event(metadata={"change_revision_id": "xyz", "domain_name": "example.com"})
        ctx = build_template_context(event)
        assert ctx["change_revision_id"] == "xyz"
        assert ctx["domain_name"] == "example.com"

    def test_empty_metadata_produces_base_keys_only(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert set(ctx.keys()) == {
            "watched_item_id",
            "item_name",
            "item_url",
            "event_type",
            "occurred_at",
            "occurred_at_iso",
            "event_label",
            "change_url",
        }

    def test_event_label_matches_event_titles(self):
        for et in WatchEventType:
            event = make_event(event_type=et)
            ctx = build_template_context(event)
            assert ctx["event_label"] == EVENT_TITLES[et.value]

    def test_occurred_at_iso_uses_z_suffix_for_utc(self):
        event = make_event()
        ctx = build_template_context(event)
        assert ctx["occurred_at_iso"] == "2026-04-14T12:00:00Z"

    def test_occurred_at_iso_preserves_microseconds(self):
        event = make_event()
        event = WatchEvent(
            event_type=event.event_type,
            watched_item_id=event.watched_item_id,
            item_name=event.item_name,
            item_url=event.item_url,
            occurred_at=datetime(2026, 4, 23, 0, 38, 33, 123456, tzinfo=UTC),
            metadata=event.metadata,
        )
        ctx = build_template_context(event)
        assert ctx["occurred_at_iso"] == "2026-04-23T00:38:33.123456Z"

    def test_occurred_at_iso_normalises_naive_to_utc(self):
        """Defensive: if a producer ever emits a naive datetime, treat it as
        UTC so the output still carries `Z` rather than silently dropping the
        timezone indicator."""
        event = make_event()
        event = WatchEvent(
            event_type=event.event_type,
            watched_item_id=event.watched_item_id,
            item_name=event.item_name,
            item_url=event.item_url,
            occurred_at=datetime(2026, 4, 14, 12, 0, 0),  # naive
            metadata=event.metadata,
        )
        ctx = build_template_context(event)
        assert ctx["occurred_at_iso"] == "2026-04-14T12:00:00Z"

    def test_change_url_populated_when_change_revision_id_present(self):
        event = make_event(metadata={"change_revision_id": "01HV0000000000000000000099"})
        ctx = build_template_context(event)
        assert ctx["change_url"] == f"https://watcher.exe.xyz/watched-items/{event.watched_item_id}"

    def test_change_url_empty_when_change_revision_id_absent(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert ctx["change_url"] == ""

    def test_derived_fields_take_precedence_over_metadata(self):
        """Hostile metadata keys must not clobber derived fields."""
        event = make_event(
            metadata={
                "change_revision_id": "01HV0000000000000000000099",
                "event_label": "BOGUS",
                "occurred_at_iso": "BOGUS",
                "change_url": "BOGUS",
            }
        )
        ctx = build_template_context(event)
        assert ctx["event_label"] == EVENT_TITLES[event.event_type.value]
        assert ctx["occurred_at_iso"] == "2026-04-14T12:00:00Z"
        assert ctx["change_url"] == (
            f"https://watcher.exe.xyz/watched-items/{event.watched_item_id}"
        )


class TestBuildBodyWithTemplates:
    def test_body_template_overrides_default_body_and_toggles(self):
        """Custom body_template replaces the entire default body — toggles
        are not applied. This is the power-user escape hatch."""
        event = make_event(metadata={"domain_name": "example.com"})
        opts = ContentOptions(include_domain=True, body_template="custom: {{ item_name }}")
        body = build_body(event, opts)
        assert body == "custom: Test Watch"
        assert "DOMAIN" not in body

    def test_body_template_none_uses_default_body(self):
        event = make_event(metadata={"domain_name": "example.com"})
        opts = ContentOptions(include_domain=True, body_template=None)
        body = build_body(event, opts)
        assert "DOMAIN: example.com" in body

    def test_body_template_bad_syntax_falls_back_to_template_string(self):
        event = make_event()
        opts = ContentOptions(body_template="{{ unclosed")
        body = build_body(event, opts)
        assert body == "{{ unclosed"

    def test_change_url_available_in_custom_template(self):
        """change_url survives as a template variable for custom bodies."""
        event = make_event(metadata={"change_revision_id": "01HV0000000000000000000099"})
        opts = ContentOptions(body_template="link: {{ change_url }}")
        body = build_body(event, opts)
        assert body == f"link: https://watcher.exe.xyz/watched-items/{WATCH_ID}"


class TestBuildTitle:
    def test_uses_default_template_for_event_type(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        title = build_title(event, ContentOptions())
        # Default title carries the [Watcher] prefix for cross-service filtering.
        assert title == "[Watcher] Change: Test Watch"

    def test_user_title_template_overrides_default(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        opts = ContentOptions(title_template="[{{ item_name }}] custom")
        title = build_title(event, opts)
        assert title == "[Test Watch] custom"

    def test_renders_event_label_for_every_event_type(self):
        for et in WatchEventType:
            event = make_event(event_type=et)
            title = build_title(event, ContentOptions())
            assert title == f"[Watcher] {EVENT_TITLES[et.value]}: Test Watch"

    def test_bad_user_template_falls_back_to_raw_string(self):
        """Preserves dispatch-never-breaks guarantee inherited from render_template."""
        event = make_event()
        opts = ContentOptions(title_template="{{ unclosed")
        title = build_title(event, opts)
        assert title == "{{ unclosed"


class TestBuildTitleStrict:
    def test_strict_raises_on_bad_user_title_template(self):
        event = make_event()
        opts = ContentOptions(title_template="{{ unknown_var }}")
        with pytest.raises(UndefinedError):
            build_title(event, opts, strict=True)

    def test_strict_still_renders_valid_default(self):
        event = make_event()
        title = build_title(event, ContentOptions(), strict=True)
        assert title == "[Watcher] Change: Test Watch"


class TestBuildBodyStrict:
    def test_strict_raises_on_bad_user_body_template(self):
        event = make_event()
        opts = ContentOptions(body_template="{{ undefined_thing }}")
        with pytest.raises(UndefinedError):
            build_body(event, opts, strict=True)

    def test_strict_renders_default_body(self):
        """change_detected default body is composed in pure Python — strict
        mode is irrelevant on this code path but must not regress."""
        event = make_event(metadata={"domain_name": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True), strict=True)
        assert "URL: https://example.com" in body
        assert "DOMAIN: example.com" in body


class TestRenderTemplateStrict:
    def test_renders_successfully(self):
        result = render_template_strict("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_raises_on_syntax_error(self):
        with pytest.raises(TemplateError):
            render_template_strict("{{ unclosed", {})

    def test_raises_on_undefined_variable(self):
        with pytest.raises(UndefinedError):
            render_template_strict("{{ unknown_var }}", {})
