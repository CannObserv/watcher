"""Tests for the notification content builder."""

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
        watch_id=WATCH_ID,
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

# Canonical canned diff used across diff/snippet tests. Real unified-diff output
# from compute_unified_diff so the rendering pipeline (fence + truncation) gets
# realistic input.
SAMPLE_PREV = "alpha\nbeta\ngamma\ndelta\nepsilon\n"
SAMPLE_CURR = "alpha\nbeta-changed\ngamma\ndelta\nepsilon\nzeta\n"


def _sample_unified_diff() -> str:
    from src.core.diff.textual import compute_unified_diff

    return compute_unified_diff(SAMPLE_PREV, SAMPLE_CURR).unified_diff


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
    interleaving toggle-driven sections into an always-present skeleton at
    the issue #104 layout positions."""

    def test_default_skeleton_with_no_toggles(self):
        """With every toggle off and no extra metadata, the body is the rich
        skeleton: header (URL, TIMESTAMP, WATCH) + body block (label,
        change_summary). No optional slots render."""
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions())
        expected = (
            "Test Watch\n"
            "URL: https://example.com\n"
            "TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"WATCH: https://watcher.exe.xyz/watches/{WATCH_ID}"
            "\n\n"
            "Change Detected\n"
            "1 added, 1 modified, 1 removed"
        )
        assert body == expected

    def test_watch_link_unconditional(self):
        """WATCH dashboard link is part of the always-present skeleton — there
        is no toggle to suppress it (issue #104, see also Q4 of the design
        interview)."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        assert f"WATCH: https://watcher.exe.xyz/watches/{WATCH_ID}" in body

    def test_full_layout_with_every_toggle_on_matches_issue_format(self):
        """With every toggle on and full metadata, the body matches the issue
        #104 layout exactly: DOMAIN between watch_name and URL; CHANGE
        between WATCH and the body block; diff after change_summary;
        INTERVAL/LAST CHANGED/SIGNIFICANCE grouped; DESCRIPTION + TAGS last."""
        event = make_event(
            metadata={
                **CHANGE_META,
                "change_id": "01HV0000000000000000000099",
                "effective_domain": "example.com",
                "check_interval": "1h",
                "last_changed_at": "2026-04-09",
                "significance": 0.5,
                "description": "Watch for license renewals",
                "tags": ["cannabis", "license"],
            }
        )
        opts = ContentOptions(
            include_diff_full=True,
            include_temporal_context=True,
            include_domain=True,
            include_last_changed_at=True,
            include_significance=True,
            include_change_dashboard_url=True,
            include_description=True,
            include_tags=True,
        )
        body = build_body(event, opts, unified_diff=_sample_unified_diff())
        expected = (
            "Test Watch\n"
            "DOMAIN: example.com\n"
            "URL: https://example.com\n"
            "TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"WATCH: https://watcher.exe.xyz/watches/{WATCH_ID}\n"
            f"CHANGE: https://watcher.exe.xyz/watches/{WATCH_ID}/changes/01HV0000000000000000000099"
            "\n\n"
            "Change Detected\n"
            "1 added, 1 modified, 1 removed"
            "\n\n"
            "```diff\n"
            "--- content\n"
            "+++ content\n"
            "@@ -1,5 +1,6 @@\n"
            " alpha\n"
            "-beta\n"
            "+beta-changed\n"
            " gamma\n"
            " delta\n"
            " epsilon\n"
            "+zeta\n"
            "```"
            "\n\n"
            "INTERVAL: 1h\n"
            "LAST CHANGED: 2026-04-09\n"
            "SIGNIFICANCE: 50%"
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
        event = make_event(metadata={"effective_domain": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True))
        # DOMAIN appears between watch_name (line 0) and URL (line 2).
        lines = body.split("\n")
        assert lines[0] == "Test Watch"
        assert lines[1] == "DOMAIN: example.com"
        assert lines[2].startswith("URL:")

    def test_omitted_when_toggle_off(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        body = build_body(event, ContentOptions(include_domain=False))
        assert "DOMAIN" not in body

    def test_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "DOMAIN" not in body


class TestChangeUrlSlot:
    CHANGE_ID = "01HV0000000000000000000099"

    def test_renders_after_watch_when_toggle_on_and_change_id_present(self):
        event = make_event(metadata={"change_id": self.CHANGE_ID})
        body = build_body(event, ContentOptions(include_change_dashboard_url=True))
        # CHANGE appears immediately after WATCH (issue layout).
        watch_idx = body.index("WATCH:")
        change_idx = body.index("CHANGE:")
        assert change_idx > watch_idx
        assert (
            f"CHANGE: https://watcher.exe.xyz/watches/{WATCH_ID}/changes/{self.CHANGE_ID}" in body
        )

    def test_omitted_when_toggle_off(self):
        event = make_event(metadata={"change_id": self.CHANGE_ID})
        body = build_body(event, ContentOptions(include_change_dashboard_url=False))
        assert "CHANGE:" not in body

    def test_omitted_when_change_id_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_change_dashboard_url=True))
        assert "CHANGE:" not in body


class TestDiffSlot:
    """The diff slot renders the unified-diff text fed in via `unified_diff=`,
    wrapped in a Markdown ```diff fenced block — this replaces the old
    chunk-label summary."""

    def test_snippet_renders_after_change_summary(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(
            event, ContentOptions(include_diff_snippet=True), unified_diff=_sample_unified_diff()
        )
        # Diff sits after the body block (change_summary).
        summary_idx = body.index("1 added, 1 modified, 1 removed")
        diff_idx = body.index("```diff")
        assert diff_idx > summary_idx
        assert "-beta" in body
        assert "+beta-changed" in body
        assert "+zeta" in body

    def test_snippet_respects_diff_snippet_lines_cap_at_hunk_boundary(self):
        """diff_snippet_lines caps lines but never truncates mid-hunk —
        when set below the first hunk's full size, only file headers + the
        @@ line are emitted, with the truncation footer."""
        # Build a diff with two hunks so we can verify hunk-boundary behavior.
        prev = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\nm\nn\no\np\nq\nr\ns\nt\nu\n"
        curr = "a\nB\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\nm\nn\nO\np\nq\nr\ns\nt\nu\n"
        from src.core.diff.textual import compute_unified_diff

        diff = compute_unified_diff(prev, curr).unified_diff
        event = make_event(metadata=CHANGE_META)
        # Cap below first hunk size — only headers + @@ should fit.
        body = build_body(
            event,
            ContentOptions(include_diff_snippet=True, diff_snippet_lines=4),
            unified_diff=diff,
        )
        assert "```diff" in body
        assert "more line" in body  # truncation footer

    def test_full_supersedes_snippet_cap(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(
            event,
            ContentOptions(include_diff_full=True, include_diff_snippet=True, diff_snippet_lines=1),
            unified_diff=_sample_unified_diff(),
        )
        # Full diff includes every change line — no truncation footer.
        assert "+beta-changed" in body
        assert "+zeta" in body
        assert "more line" not in body

    def test_omitted_when_both_toggles_off(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(), unified_diff=_sample_unified_diff())
        assert "```diff" not in body

    def test_omitted_when_unified_diff_missing(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_snippet=True), unified_diff=None)
        assert "```diff" not in body

    def test_omitted_when_unified_diff_empty(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_snippet=True), unified_diff="")
        assert "```diff" not in body


class TestStatsSlots:
    def test_interval_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"check_interval": "1h"})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "INTERVAL: 1h" in body

    def test_last_changed_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"last_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "LAST CHANGED: 2026-04-09" in body

    def test_significance_rendered_as_int_percent(self):
        event = make_event(metadata={"significance": 0.73})
        body = build_body(event, ContentOptions(include_significance=True))
        assert "SIGNIFICANCE: 73%" in body

    def test_zero_significance_renders(self):
        event = make_event(metadata={"significance": 0.0})
        body = build_body(event, ContentOptions(include_significance=True))
        assert "SIGNIFICANCE: 0%" in body

    def test_full_significance_renders(self):
        event = make_event(metadata={"significance": 1.0})
        body = build_body(event, ContentOptions(include_significance=True))
        assert "SIGNIFICANCE: 100%" in body

    def test_stats_grouped_in_one_paragraph(self):
        """INTERVAL / LAST CHANGED / SIGNIFICANCE share a single paragraph
        per the issue layout — no blank lines between them."""
        event = make_event(
            metadata={"check_interval": "1h", "last_changed_at": "2026-04-09", "significance": 0.5}
        )
        body = build_body(
            event,
            ContentOptions(
                include_temporal_context=True,
                include_last_changed_at=True,
                include_significance=True,
            ),
        )
        # The three lines should appear consecutively without intervening blanks.
        assert "INTERVAL: 1h\nLAST CHANGED: 2026-04-09\nSIGNIFICANCE: 50%" in body

    def test_stats_omitted_when_toggles_off(self):
        event = make_event(
            metadata={"check_interval": "1h", "last_changed_at": "2026-04-09", "significance": 0.5}
        )
        body = build_body(event, ContentOptions())
        assert "INTERVAL" not in body
        assert "LAST CHANGED" not in body
        assert "SIGNIFICANCE" not in body

    def test_stats_omitted_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(
            event,
            ContentOptions(
                include_temporal_context=True,
                include_last_changed_at=True,
                include_significance=True,
            ),
        )
        assert "INTERVAL" not in body
        assert "LAST CHANGED" not in body
        assert "SIGNIFICANCE" not in body


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
            "occurred_at_iso",
            "event_label",
            "change_summary",
            "change_url",
            "diff_snippet",
            "diff_full",
            "chunks_changed",
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
            watch_id=event.watch_id,
            watch_name=event.watch_name,
            watch_url=event.watch_url,
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
            watch_id=event.watch_id,
            watch_name=event.watch_name,
            watch_url=event.watch_url,
            occurred_at=datetime(2026, 4, 14, 12, 0, 0),  # naive
            metadata=event.metadata,
        )
        ctx = build_template_context(event)
        assert ctx["occurred_at_iso"] == "2026-04-14T12:00:00Z"

    def test_change_summary_counts_changes(self):
        event = make_event(metadata={"added": ["a", "b"], "modified": [{}], "removed": []})
        ctx = build_template_context(event)
        assert ctx["change_summary"] == "2 added, 1 modified"

    def test_change_summary_details_pending_when_empty(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED, metadata={})
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

    def test_diff_snippet_populated_when_unified_diff_present(self):
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event, unified_diff=_sample_unified_diff())
        assert ctx["diff_snippet"].startswith("```diff\n")
        assert ctx["diff_snippet"].rstrip("\n").endswith("```")
        assert "-beta" in ctx["diff_snippet"]
        assert "+beta-changed" in ctx["diff_snippet"]

    def test_diff_full_populated_when_unified_diff_present(self):
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event, unified_diff=_sample_unified_diff())
        assert ctx["diff_full"].startswith("```diff\n")
        # Full version contains the second-hunk change (the appended `zeta`).
        assert "+zeta" in ctx["diff_full"]

    def test_diff_snippet_capped_at_default(self):
        """Default snippet cap (25 lines) caps long diffs; diff_full keeps all."""
        # Build a long synthetic diff: 50 line changes
        prev_lines = [f"line-{i}" for i in range(50)]
        curr_lines = [f"changed-{i}" if i % 2 == 0 else f"line-{i}" for i in range(50)]
        from src.core.diff.textual import compute_unified_diff

        long_diff = compute_unified_diff(
            "\n".join(prev_lines) + "\n", "\n".join(curr_lines) + "\n"
        ).unified_diff
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event, unified_diff=long_diff)
        snippet_lines = ctx["diff_snippet"].count("\n") + 1
        full_lines = ctx["diff_full"].count("\n") + 1
        # Snippet has the truncation footer; cap+fence overhead means a few more
        # than 25 lines total, but it's strictly smaller than the full diff.
        assert snippet_lines < full_lines
        assert "more line" in ctx["diff_snippet"]
        assert "more line" not in ctx["diff_full"]

    def test_diff_snippet_empty_when_no_unified_diff(self):
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event, unified_diff=None)
        assert ctx["diff_snippet"] == ""
        assert ctx["diff_full"] == ""

    def test_diff_snippet_empty_when_unified_diff_blank(self):
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event, unified_diff="")
        assert ctx["diff_snippet"] == ""
        assert ctx["diff_full"] == ""

    def test_chunks_changed_structured(self):
        """chunks_changed is a list of {status, label, similarity} dicts."""
        event = make_event(metadata=CHANGE_META)
        ctx = build_template_context(event)
        assert ctx["chunks_changed"] == [
            {"status": "added", "label": "Licenses", "similarity": None},
            {"status": "removed", "label": "Hours", "similarity": None},
            {"status": "modified", "label": "Contact Info", "similarity": 0.85},
        ]

    def test_chunks_changed_empty_when_no_chunk_metadata(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert ctx["chunks_changed"] == []

    def test_chunks_changed_skips_modified_without_label(self):
        event = make_event(metadata={"modified": [{"similarity": 0.9}]})
        ctx = build_template_context(event)
        assert ctx["chunks_changed"] == []

    def test_derived_fields_take_precedence_over_metadata(self):
        """Hostile metadata keys must not clobber derived fields."""
        event = make_event(
            metadata={
                "change_id": "01HV0000000000000000000099",
                "event_label": "BOGUS",
                "occurred_at_iso": "BOGUS",
                "change_summary": "BOGUS",
                "change_url": "BOGUS",
                "diff_snippet": "BOGUS",
                "diff_full": "BOGUS",
                "chunks_changed": "BOGUS",
            }
        )
        ctx = build_template_context(event)
        assert ctx["event_label"] == EVENT_TITLES[event.event_type.value]
        assert ctx["occurred_at_iso"] == "2026-04-14T12:00:00Z"
        assert ctx["change_summary"] == "details pending"
        assert ctx["change_url"] == (
            f"https://watcher.exe.xyz/watches/{event.watch_id}/changes/01HV0000000000000000000099"
        )
        assert ctx["diff_snippet"] == ""
        assert ctx["diff_full"] == ""
        assert ctx["chunks_changed"] == []


class TestBuildBodyWithTemplates:
    def test_body_template_overrides_default_body_and_toggles(self):
        """Custom body_template replaces the entire default body — toggles
        are not applied. This is the power-user escape hatch."""
        event = make_event(metadata={"effective_domain": "example.com"})
        opts = ContentOptions(include_domain=True, body_template="custom: {{ watch_name }}")
        body = build_body(event, opts)
        assert body == "custom: Test Watch"
        assert "DOMAIN" not in body

    def test_body_template_none_uses_default_body(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        opts = ContentOptions(include_domain=True, body_template=None)
        body = build_body(event, opts)
        assert "DOMAIN: example.com" in body

    def test_body_template_bad_syntax_falls_back_to_template_string(self):
        event = make_event()
        opts = ContentOptions(body_template="{{ unclosed")
        body = build_body(event, opts)
        assert body == "{{ unclosed"

    def test_diff_snippet_in_custom_template_uses_user_cap(self):
        """User-set diff_snippet_lines must take effect even on the
        body_template path. build_template_context applies the module
        default; build_body overrides it with options.diff_snippet_lines so
        a custom template referencing {{ diff_snippet }} honors the
        preference."""
        # Multi-hunk diff so the cap actually matters.
        prev_lines = [f"line-{i}" for i in range(50)]
        curr_lines = [f"changed-{i}" if i % 5 == 0 else f"line-{i}" for i in range(50)]
        from src.core.diff.textual import compute_unified_diff

        long_diff = compute_unified_diff(
            "\n".join(prev_lines) + "\n", "\n".join(curr_lines) + "\n"
        ).unified_diff

        event = make_event(metadata=CHANGE_META)
        opts = ContentOptions(body_template="{{ diff_snippet }}", diff_snippet_lines=4)
        body = build_body(event, opts, unified_diff=long_diff)
        # User cap honored: tight cap → truncation footer present.
        assert "more line" in body

        # And without a cap override, full diff fits without truncation.
        opts_full = ContentOptions(body_template="{{ diff_full }}")
        body_full = build_body(event, opts_full, unified_diff=long_diff)
        assert "more line" not in body_full


class TestBuildTitle:
    def test_uses_default_template_for_event_type(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        title = build_title(event, ContentOptions())
        # Default title carries the [Observo] prefix for cross-service filtering.
        assert title == "[Observo] Change Detected: Test Watch"

    def test_user_title_template_overrides_default(self):
        event = make_event(event_type=WatchEventType.CHANGE_DETECTED)
        opts = ContentOptions(title_template="[{{ watch_name }}] custom")
        title = build_title(event, opts)
        assert title == "[Test Watch] custom"

    def test_renders_event_label_for_every_event_type(self):
        for et in WatchEventType:
            event = make_event(event_type=et)
            title = build_title(event, ContentOptions())
            assert title == f"[Observo] {EVENT_TITLES[et.value]}: Test Watch"

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
        assert title == "[Observo] Change Detected: Test Watch"


class TestBuildBodyStrict:
    def test_strict_raises_on_bad_user_body_template(self):
        event = make_event()
        opts = ContentOptions(body_template="{{ undefined_thing }}")
        with pytest.raises(UndefinedError):
            build_body(event, opts, strict=True)

    def test_strict_renders_default_body(self):
        """change_detected default body is composed in pure Python — strict
        mode is irrelevant on this code path but must not regress."""
        event = make_event(metadata={"effective_domain": "example.com", **CHANGE_META})
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
