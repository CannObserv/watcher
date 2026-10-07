"""Tests for the notification content builder.

#221 stripped the diff/significance/change_summary machinery; #222 restores the
diff (snippet + full) over the processor-stored canonical text. The
change_detected body is the header skeleton plus the Context toggles (Domain,
Last changed, Check interval, Description, Tags) as one Markdown list, then the
diff as a fenced block. The header link is labelled ITEM (was WATCH).
"""

from datetime import UTC, datetime

import pytest
from jinja2 import TemplateError, UndefinedError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import (
    MAX_RENDERED_DIFF_BYTES,
    SPEC_CHANGED_NOTE,
    _truncate_hunks,
    build_body,
    build_template_context,
    build_title,
    render_template,
    render_template_strict,
    resolve_options,
)
from src.core.notifications.default_templates import (
    DEFAULT_BODY_TEMPLATES,
    compose_body_prefill,
)
from src.core.notifications.diff import ChangeDiff, compute_change_diff
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.public_base_url import PUBLIC_BASE_URL_ENV

OCCURRED_AT = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)
WATCH_ID = "01HV0000000000000000000001"
#: A host that is no deployment's, so no expected string here can be mistaken
#: for where the dashboard actually lives (#296 D6).
BASE = "https://watcher.test"


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """Every test in this module renders against a configured base unless it
    removes it; ``tests/conftest.py`` clears the variable at import."""
    monkeypatch.setenv(PUBLIC_BASE_URL_ENV, BASE)


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
        """With every toggle off, the body is the header skeleton alone rendered
        as a Markdown bullet list: item_name, URL, TIMESTAMP, ITEM. The list
        form (#225) keeps each fact on its own line on HTML-email channels,
        which render the source Markdown through CommonMark (#224)."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        expected = (
            "- Test Watch\n"
            "- URL: https://example.com\n"
            "- TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"- ITEM: {BASE}/watched-items/{WATCH_ID}"
        )
        assert body == expected

    def test_item_link_is_not_a_toggle(self):
        """The ITEM dashboard link is part of the always-present skeleton —
        there is no toggle to suppress it. Only an unconfigured base removes
        it (below)."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        assert f"ITEM: {BASE}/watched-items/{WATCH_ID}" in body

    def test_item_link_follows_the_configured_base(self, monkeypatch):
        """#296 D6: the host is configuration. The move to co-watcher changes
        it, and a constant was also why a dev server linked into production."""
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://co-watcher.exe.xyz/")
        body = build_body(make_event(metadata={}), ContentOptions())
        assert f"- ITEM: https://co-watcher.exe.xyz/watched-items/{WATCH_ID}" in body

    def test_item_link_is_omitted_without_a_base(self, monkeypatch):
        """No base, no link — the rest of the skeleton is untouched. A relative
        ``/watched-items/…`` is useless in Slack or email, and guessing a host
        would point readers at a VM that no longer serves the dashboard.
        Archiver's ``ARCHIVER_PUBLIC_BASE_URL`` makes the same call."""
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV)
        body = build_body(make_event(metadata={}), ContentOptions())
        assert body == (
            "- Test Watch\n- URL: https://example.com\n- TIMESTAMP: 2026-04-14T12:00:00Z"
        )

    @pytest.mark.parametrize("value", ["co-watcher.exe.xyz", "https://[co-watcher.exe.xyz"])
    def test_a_malformed_base_never_breaks_a_dispatch(self, monkeypatch, value):
        """The lifespan refuses a malformed value at startup; the render path
        must still never raise on one, because a failed dispatch is worse than a
        missing link. The bracket case is one ``urlsplit`` itself raises on."""
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, value)
        body = build_body(make_event(metadata={}), ContentOptions())
        assert "ITEM:" not in body
        assert body.startswith("- Test Watch")

    def test_full_layout_with_every_toggle_on(self):
        """With every surviving toggle on and full metadata, the body is one
        Markdown bullet list in canonical order: item_name, DOMAIN, URL, PREVIOUS
        CHANGE, INTERVAL, TIMESTAMP, ITEM, DESCRIPTION, TAGS. DESCRIPTION and
        TAGS are trailing list items (#225 folded them from separate paragraphs
        into the single list so every fact renders identically on HTML email)."""
        event = make_event(
            metadata={
                "change_revision_id": "01HV0000000000000000000099",
                "domain_name": "example.com",
                "check_interval": "1h",
                "previous_changed_at": "2026-04-09",
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
            "- Test Watch\n"
            "- DOMAIN: example.com\n"
            "- URL: https://example.com\n"
            "- PREVIOUS CHANGE: 2026-04-09\n"
            "- INTERVAL: 1h\n"
            "- TIMESTAMP: 2026-04-14T12:00:00Z\n"
            f"- ITEM: {BASE}/watched-items/{WATCH_ID}\n"
            "- DESCRIPTION: Watch for license renewals\n"
            "- TAGS: cannabis, license"
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
        event = make_event(metadata={})  # no optional sections in either path
        seed_rendered = render_template(
            DEFAULT_BODY_TEMPLATES["change_detected"], build_template_context(event)
        )
        dispatch_output = build_body(event, ContentOptions())
        assert seed_rendered == dispatch_output

    def test_seed_matches_dispatcher_output_without_a_base(self, monkeypatch):
        """#296 D6: with no base configured the composer drops the ITEM line.
        A template a user copied from the seed must drop it too — otherwise
        every dev server renders a relative ``/watched-items/…`` link, and the
        invariant above holds only when a base is set."""
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV, raising=False)
        event = make_event(metadata={})
        seed_rendered = render_template(
            DEFAULT_BODY_TEMPLATES["change_detected"], build_template_context(event)
        )
        assert "/watched-items/" not in seed_rendered
        assert seed_rendered == build_body(event, ContentOptions())
        custom = ContentOptions(body_template=compose_body_prefill("change_detected"))
        assert "ITEM:" not in build_body(event, custom)


class TestDomainSlot:
    def test_renders_between_name_and_url_when_toggle_on_and_metadata_present(self):
        event = make_event(metadata={"domain_name": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True))
        # DOMAIN appears between item_name (line 0) and URL (line 2), each a
        # Markdown list item.
        lines = body.split("\n")
        assert lines[0] == "- Test Watch"
        assert lines[1] == "- DOMAIN: example.com"
        assert lines[2].startswith("- URL:")

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

    def test_previous_change_renders_with_label_when_toggle_on(self):
        event = make_event(metadata={"previous_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "PREVIOUS CHANGE: 2026-04-09" in body

    def test_this_change_is_not_repeated_as_last_changed(self):
        """#349: on a change, ``last_changed_at`` *is* this change — the
        pipeline sets it before the event is built — so it only ever repeated
        TIMESTAMP. The toggle shows the change before this one instead."""
        event = make_event(metadata={"last_changed_at": "2026-04-14T12:00:00Z"})
        body = build_body(event, ContentOptions(include_last_changed_at=True))
        assert "LAST CHANGED" not in body
        assert "PREVIOUS CHANGE" not in body

    def test_previous_change_and_interval_render_between_url_and_timestamp(self):
        """PREVIOUS CHANGE + INTERVAL sit between URL and TIMESTAMP in the
        header, with PREVIOUS CHANGE first."""
        event = make_event(metadata={"check_interval": "1h", "previous_changed_at": "2026-04-09"})
        body = build_body(
            event,
            ContentOptions(include_temporal_context=True, include_last_changed_at=True),
        )
        assert (
            "- URL: https://example.com\n- PREVIOUS CHANGE: 2026-04-09\n- INTERVAL: 1h\n"
            "- TIMESTAMP: "
        ) in body

    def test_stats_omitted_when_toggles_off(self):
        event = make_event(metadata={"check_interval": "1h", "previous_changed_at": "2026-04-09"})
        body = build_body(event, ContentOptions())
        assert "INTERVAL" not in body
        assert "PREVIOUS CHANGE" not in body

    def test_stats_omitted_when_metadata_missing(self):
        """An item's first change has no previous one: a baseline never sets
        ``last_changed_at``."""
        event = make_event(metadata={})
        body = build_body(
            event,
            ContentOptions(
                include_temporal_context=True,
                include_last_changed_at=True,
            ),
        )
        assert "INTERVAL" not in body
        assert "PREVIOUS CHANGE" not in body


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


class TestExtractionChangedLabel:
    """Option A (D6, #326): a change seen after the bound spec moved says so.

    Not a toggle: it qualifies the change itself, so a reader who acts on the
    notification needs it whatever else they chose to see.
    """

    def test_a_spec_change_carries_the_note(self):
        event = make_event(metadata={"extraction_changed": "spec"})
        body = build_body(event, ContentOptions())
        assert f"- {SPEC_CHANGED_NOTE}" in body.split("\n")

    @pytest.mark.parametrize("value", [None, "processor"])
    def test_anything_else_carries_none(self, value):
        event = make_event(metadata={"extraction_changed": value})
        assert SPEC_CHANGED_NOTE not in build_body(event, ContentOptions())

    def test_the_note_names_the_cause(self):
        assert "source spec changed" in SPEC_CHANGED_NOTE


class TestMarkdownListContract:
    """#224/#225 regression guard: the change_detected body must be a Markdown
    bullet list.

    Dispatch moved to the Notifier service in #137; it renders the source
    Markdown through CommonMark (mistune) for HTML-native channels (Mailgun,
    SES, mailto). Under CommonMark a lone ``\\n`` is a *soft* break (a space),
    so a paragraph of ``\\n``-joined fact lines collapses onto one run-on line
    in HTML email. A bullet list is real block structure — one ``<li>`` per
    fact — with no reliance on fragile trailing-whitespace hard breaks. Watcher
    cannot import the Notifier's renderer (cross-repo), so we assert the
    contract Watcher promises: every body line is a list item.
    """

    def test_every_line_is_a_list_item_full_metadata(self):
        event = make_event(
            metadata={
                "domain_name": "example.com",
                "check_interval": "1h",
                "previous_changed_at": "2026-04-09",
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
        lines = body.split("\n")
        assert lines, "body must not be empty"
        for line in lines:
            assert line.startswith("- "), (
                f"non-list line would soft-wrap into the previous line on HTML "
                f"email (CommonMark soft break): {line!r}"
            )

    def test_skeleton_is_a_tight_list_without_paragraph_breaks(self):
        """No blank lines — a single tight bullet list. A blank line would start
        a new paragraph whose interior ``\\n`` lines would collapse again."""
        event = make_event(metadata={})
        body = build_body(event, ContentOptions())
        assert "\n\n" not in body
        assert all(line.startswith("- ") for line in body.split("\n"))


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
            "app_url",
            "change_url",
            "diff_snippet",
            "diff_full",
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
        assert ctx["change_url"] == f"{BASE}/watched-items/{event.watched_item_id}"

    def test_change_url_empty_when_change_revision_id_absent(self):
        event = make_event(metadata={})
        ctx = build_template_context(event)
        assert ctx["change_url"] == ""

    def test_change_url_empty_without_a_base(self, monkeypatch):
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV)
        event = make_event(metadata={"change_revision_id": "01HV0000000000000000000099"})
        assert build_template_context(event)["change_url"] == ""

    def test_app_url_is_in_the_context(self, monkeypatch):
        """The seed template's ITEM line is ``{{ app_url }}/watched-items/…``,
        so a custom template can build the same link — and test it with
        ``{% if app_url %}``."""
        assert build_template_context(make_event())["app_url"] == BASE
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV)
        assert build_template_context(make_event())["app_url"] == ""

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
        assert ctx["change_url"] == (f"{BASE}/watched-items/{event.watched_item_id}")


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
        assert body == f"link: {BASE}/watched-items/{WATCH_ID}"


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


#: Two hunks: four lines, then two. Rendered, an empty line separates them.
DIFF = ChangeDiff(
    hunks=(
        ("  alpha", "- beta", "+ beta-changed", "  gamma delta"),
        ("  epsilon", "+ zeta"),
    )
)


def _long_diff(n: int = 30) -> ChangeDiff:
    return ChangeDiff(hunks=tuple((f"- old-{i}", f"+ new-{i}") for i in range(n)))


def _fenced(body: str) -> list[str]:
    """The diff block's lines, fences included."""
    return body.split("\n\n", 1)[1].split("\n")


class TestDiffSlot:
    """#222: the diff renders after the fact list as a Markdown ```diff block —
    its own block, so the list contract (#225) holds for every list line."""

    def test_snippet_renders_after_the_list(self):
        body = build_body(make_event(metadata={}), ContentOptions(), diff=DIFF)
        listing, fenced = body.split("\n\n", 1)
        assert all(line.startswith("- ") for line in listing.split("\n"))
        assert fenced.startswith("```diff\n")
        assert fenced.endswith("```")
        assert "- beta" in fenced
        assert "+ zeta" in fenced

    def test_no_unified_diff_header(self):
        """#349: ``---``/``+++``/``@@`` counted internal segments, not anything
        a reader could find on the page."""
        lines = _fenced(build_body(make_event(metadata={}), ContentOptions(), diff=DIFF))
        assert not any(line.startswith(("---", "+++", "@@")) for line in lines)

    def test_hunks_are_separated_by_an_empty_line(self):
        lines = _fenced(build_body(make_event(metadata={}), ContentOptions(), diff=DIFF))
        assert lines[1:-1] == [*DIFF.hunks[0], "", *DIFF.hunks[1]]

    def test_snippet_is_capped_at_a_hunk_boundary(self):
        body = build_body(
            make_event(metadata={}),
            ContentOptions(diff_snippet_lines=6),
            diff=DIFF,
        )
        # The first hunk (4) fits beside the footer; the second is cut whole.
        assert "+ beta-changed" in body
        assert "+ zeta" not in body
        assert "... (2 more lines)" in body

    def test_cap_below_the_first_hunk_keeps_its_first_lines(self):
        """#349: every line is now readable page text, so a cut first hunk
        keeps what fits rather than a bare position marker."""
        body = build_body(make_event(metadata={}), ContentOptions(diff_snippet_lines=3), diff=DIFF)
        lines = _fenced(body)
        assert lines[1:-1] == ["  alpha", "- beta", "... (4 more lines)"]

    def test_full_supersedes_the_snippet_cap(self):
        opts = ContentOptions(include_diff_full=True, diff_snippet_lines=1)
        body = build_body(make_event(metadata={}), opts, diff=DIFF)
        assert "+ zeta" in body
        assert "more line" not in body

    def test_omitted_when_both_toggles_off(self):
        opts = ContentOptions(include_diff_snippet=False)
        body = build_body(make_event(metadata={}), opts, diff=DIFF)
        assert "```" not in body

    def test_omitted_when_no_diff_was_computed(self):
        body = build_body(make_event(metadata={}), ContentOptions(), diff=None)
        assert "```" not in body
        assert "DIFF" not in body

    def test_unavailable_is_said_as_a_list_item(self):
        body = build_body(
            make_event(metadata={}),
            ContentOptions(),
            diff=ChangeDiff(unavailable="content too large"),
        )
        assert body.split("\n")[-1] == "- DIFF: unavailable (content too large)"
        assert "```" not in body

    def test_unavailable_is_silent_when_no_diff_was_asked_for(self):
        body = build_body(
            make_event(metadata={}),
            ContentOptions(include_diff_snippet=False),
            diff=ChangeDiff(unavailable="content too large"),
        )
        assert "DIFF" not in body

    def test_the_spec_note_stays_in_the_list_above_the_diff(self):
        event = make_event(metadata={"extraction_changed": "spec"})
        listing, _fenced_block = build_body(event, ContentOptions(), diff=DIFF).split("\n\n", 1)
        assert listing.split("\n")[-1] == f"- {SPEC_CHANGED_NOTE}"

    def test_non_change_events_never_carry_a_diff(self):
        event = make_event(WatchEventType.WATCH_ERROR, metadata={"status_code": 500})
        assert "```" not in build_body(event, ContentOptions(include_diff_full=True), diff=DIFF)


class TestRenderedSizeBackstop:
    """#346, built in #349: a word diff scales with the change, but a rewrite
    is a big change, and one huge token defeats a line cap. Every rendering is
    bounded in bytes, so an oversized body can never cost a recipient their
    notification."""

    def _inner(self, body: str) -> str:
        return "\n".join(_fenced(body)[1:-1])

    def test_a_rewrite_is_cut_on_a_hunk_boundary(self):
        hunk = tuple(f"+ {'word ' * 12}{i}" for i in range(20))
        diff = ChangeDiff(hunks=(hunk,) * 100)
        body = build_body(
            make_event(metadata={}), ContentOptions(include_diff_full=True), diff=diff
        )
        inner = self._inner(body)
        assert len(inner.encode()) <= MAX_RENDERED_DIFF_BYTES
        kept = inner.split("\n... (")[0].split("\n")
        assert len([line for line in kept if line]) % len(hunk) == 0
        assert inner.endswith(" more lines)")

    def test_one_enormous_line_is_cut_within_the_line(self):
        diff = ChangeDiff(hunks=(("  before", "+ " + "é" * MAX_RENDERED_DIFF_BYTES),))
        body = build_body(
            make_event(metadata={}), ContentOptions(include_diff_full=True), diff=diff
        )
        inner = self._inner(body)
        assert len(inner.encode()) <= MAX_RENDERED_DIFF_BYTES
        assert inner.split("\n")[1].endswith("é…")

    def test_the_snippet_is_bounded_too(self):
        diff = ChangeDiff(hunks=(("+ " + "x" * 100_000,),))
        ctx = build_template_context(make_event(metadata={}), diff=diff)
        assert len(ctx["diff_snippet"].encode()) <= MAX_RENDERED_DIFF_BYTES + 64

    def test_a_diff_within_the_cap_is_untouched(self):
        body = build_body(
            make_event(metadata={}), ContentOptions(include_diff_full=True), diff=DIFF
        )
        assert "more line" not in body


class TestDiffFenceCannotBeClosedByContent:
    """CR 13/14: the diff carries the watched page's own text into a body the
    notifier renders as Markdown. CommonMark closes a backtick fence on a line
    that is *only* a run at least as long, indented 0–3 spaces — and a context
    line is the page's text behind two spaces. A page line that is exactly
    three backticks would end a three-backtick block and let the next line
    render as live Markdown in recipients' email. The fence must outrun every
    backtick run in the content. #349 kept the page's text inside the fence
    (line pairs, not inline marks) so this stays the one boundary.
    """

    HOSTILE = ChangeDiff(
        hunks=(("  ```", "  ![x](https://tracker.example/p.png)", "- old", "+ new"),)
    )

    @staticmethod
    def _closes(line: str, fence: str) -> bool:
        """CommonMark's closing rule: up to three spaces, then a run at least
        as long as the opening fence, then nothing but spaces."""
        stripped = line.strip(" ")
        return (
            len(line) - len(line.lstrip(" ")) <= 3
            and stripped.startswith(fence)
            and set(stripped) == {"`"}
        )

    def test_a_content_line_that_would_close_three_backticks_cannot_close_the_fence(self):
        lines = _fenced(build_body(make_event(metadata={}), ContentOptions(), diff=self.HOSTILE))
        fence = lines[-1]
        assert lines[0] == f"{fence}diff"
        assert fence == "````"
        assert not any(self._closes(line, fence) for line in lines[1:-1])

    def test_a_hostile_page_stays_inside_the_fence_end_to_end(self):
        """Hostile page text through the real diff: a chunk that is a bare
        fence, an image beacon, raw HTML, and a longer run in the change."""
        previous = (
            b"intro\n```\n![x](https://tracker.example/p.png)\n<script>alert(1)</script>\nold tail"
        )
        current = previous.replace(b"old tail", b"`````\n# heading\nnew tail")
        body = build_body(
            make_event(metadata={}),
            ContentOptions(include_diff_full=True),
            diff=compute_change_diff(previous, current),
        )
        lines = _fenced(body)
        fence = lines[-1]
        assert lines[0] == f"{fence}diff"
        assert fence == "``````"
        inner = lines[1:-1]
        assert not any(self._closes(line, fence) for line in inner)
        assert "+ # heading" in inner
        assert "  <script>alert(1)</script>" in inner

    def test_plain_content_keeps_the_three_backtick_fence(self):
        lines = _fenced(build_body(make_event(metadata={}), ContentOptions(), diff=DIFF))
        assert (lines[0], lines[-1]) == ("```diff", "```")

    def test_template_variables_use_the_same_fence(self):
        ctx = build_template_context(make_event(metadata={}), diff=self.HOSTILE)
        assert ctx["diff_full"].startswith("````diff\n")
        assert ctx["diff_snippet"].endswith("\n````")


class TestDiffTemplateVariables:
    def test_snippet_and_full_are_fenced(self):
        ctx = build_template_context(make_event(metadata={}), diff=DIFF)
        assert ctx["diff_snippet"].startswith("```diff\n")
        assert ctx["diff_full"].startswith("```diff\n")
        assert "+ zeta" in ctx["diff_full"]

    def test_snippet_capped_at_the_default_full_never(self):
        ctx = build_template_context(make_event(metadata={}), diff=_long_diff())
        assert "more line" in ctx["diff_snippet"]
        assert "more line" not in ctx["diff_full"]

    def test_empty_without_a_diff(self):
        ctx = build_template_context(make_event(metadata={}))
        assert ctx["diff_snippet"] == ""
        assert ctx["diff_full"] == ""

    def test_unavailable_says_why(self):
        ctx = build_template_context(
            make_event(metadata={}), diff=ChangeDiff(unavailable="previous text not stored")
        )
        assert ctx["diff_snippet"] == "(diff unavailable: previous text not stored)"
        assert ctx["diff_full"] == "(diff unavailable: previous text not stored)"

    def test_custom_template_honours_the_user_cap(self):
        opts = ContentOptions(body_template="{{ diff_snippet }}", diff_snippet_lines=4)
        assert "more line" in build_body(make_event(metadata={}), opts, diff=_long_diff())
        opts_full = ContentOptions(body_template="{{ diff_full }}")
        assert "more line" not in build_body(make_event(metadata={}), opts_full, diff=_long_diff())

    def test_metadata_cannot_clobber_the_diff(self):
        ctx = build_template_context(make_event(metadata={"diff_full": "spoof"}), diff=DIFF)
        assert ctx["diff_full"].startswith("```diff")


class TestTruncateHunks:
    HUNKS = DIFF.hunks  # 4 lines, separator, 2 lines

    def test_fits_untouched(self):
        assert _truncate_hunks(self.HUNKS, max_lines=7, max_bytes=1000) == (
            [*self.HUNKS[0], "", *self.HUNKS[1]],
            0,
        )

    def test_whole_hunks_only(self):
        assert _truncate_hunks(self.HUNKS, max_lines=6, max_bytes=1000) == (
            list(self.HUNKS[0]),
            2,
        )

    def test_first_hunk_too_big_keeps_its_first_lines(self):
        assert _truncate_hunks(self.HUNKS, max_lines=3, max_bytes=1000) == (
            ["  alpha", "- beta"],
            4,
        )

    def test_a_one_line_cap_keeps_only_the_footer(self):
        assert _truncate_hunks(self.HUNKS, max_lines=1, max_bytes=1000) == ([], 6)

    def test_bytes_bound_without_a_line_cap(self):
        """The footer counts against the budget: the cut body plus its footer
        stays within ``max_bytes``."""
        hunks = (("+ " + "a" * 40,), ("+ " + "b" * 40,))
        assert _truncate_hunks(hunks, max_lines=None, max_bytes=80) == ([hunks[0][0]], 1)
