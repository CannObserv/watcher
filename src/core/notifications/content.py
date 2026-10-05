"""Notification body builder — resolves ContentOptions and composes custom bodies."""

import os

from jinja2 import Environment, StrictUndefined, TemplateError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.default_templates import (
    CHANGE_DETECTED_HEADER_LINES,
    CHANGE_DETECTED_ITEM_LINE,
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.diff import ChangeDiff
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.public_base_url import PublicBaseUrlInvalid, public_base_url
from src.core.utils import format_utc_iso

_jinja_env = Environment(autoescape=False)
_jinja_env_strict = Environment(autoescape=False, undefined=StrictUndefined)

# Default cap for the `diff_snippet` template variable. Lifted from the
# Pydantic field default so the two stay in lockstep — when a custom user
# template references `{{ diff_snippet }}`, they get a sensibly-bounded slice
# rather than a wall of unified-diff lines. Use `{{ diff_full }}` for the
# unbounded version.
_DEFAULT_DIFF_SNIPPET_CAP: int = ContentOptions.model_fields["diff_snippet_lines"].default


def render_template(template_str: str, context: dict) -> str:
    """Render a Jinja2 template string with the given context.

    Returns the rendered string on success, or the original template_str
    (unchanged) if any Jinja2 error occurs. This ensures notification dispatch
    is never silently broken by a bad template.
    """
    try:
        tmpl = _jinja_env.from_string(template_str)
        return tmpl.render(context)
    except TemplateError:
        return template_str


def render_template_strict(template_str: str, context: dict) -> str:
    """Render a Jinja2 template string, raising on any template error.

    Uses a separate Jinja2 environment with StrictUndefined so that undefined
    variable references (typos like ``{{ unnkown }}``) raise UndefinedError
    instead of silently rendering as the empty string. Syntax errors and
    other TemplateError subclasses propagate too.

    Use only where the user expects to see template errors — e.g. the preview
    endpoint. Dispatch uses `render_template` so a bad template never breaks
    a real notification.
    """
    tmpl = _jinja_env_strict.from_string(template_str)
    return tmpl.render(context)


def build_template_context(
    event: WatchEvent,
    *,
    diff: ChangeDiff | None = None,
    diff_snippet_cap: int = _DEFAULT_DIFF_SNIPPET_CAP,
) -> dict:
    """Build Jinja2 template context from a WatchEvent.

    Includes metadata keys flattened in, plus derived fields that the default
    templates rely on:
      - `event_label` — human-readable event title (always set)
      - `occurred_at_iso` — ISO 8601 UTC timestamp (`...Z`), AGENTS.md format
      - `app_url` — the dashboard's public base (`WATCHER_PUBLIC_BASE_URL`,
        #296 D6); empty when not configured
      - `change_url` — WatchedItem dashboard URL when `change_revision_id` is in
        metadata and a base is configured; empty otherwise
      - `diff_snippet` — Markdown ```diff fenced unified diff, capped at
        `diff_snippet_cap` lines (hunk-boundary aware)
      - `diff_full` — the same, uncapped

    Both diff fields are empty without a `diff` (none computed: not a change,
    or no recipient asked) and read `(diff unavailable: <reason>)` when one was
    attempted and could not be made (#222). The dispatcher loads `diff` from
    the stored canonical texts (`diff_loader.load_change_diff`); the preview computes
    it from canned text (`preview_fixtures.preview_diff`). `change_summary` and
    `chunks_changed` stay retired: the canonical text keeps no chunk boundaries
    (#222 D8).

    Derived fields are written *after* `metadata.update()` so that an event
    metadata dict that happens to share a key cannot clobber the value the
    template builder computed.
    """
    ctx = {
        "watched_item_id": event.watched_item_id,
        "item_name": event.item_name,
        "item_url": event.item_url,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
    }
    ctx.update(event.metadata)
    # Derived fields take precedence over any same-named metadata keys.
    ctx["event_label"] = EVENT_TITLES[event.event_type.value]
    ctx["occurred_at_iso"] = format_utc_iso(event.occurred_at)
    ctx["app_url"] = _dashboard_base()
    ctx["change_url"] = _format_change_url(
        event.watched_item_id, event.metadata.get("change_revision_id"), ctx["app_url"]
    )
    ctx["diff_snippet"] = _render_diff_variable(diff, max_lines=diff_snippet_cap)
    ctx["diff_full"] = _render_diff_variable(diff, max_lines=None)
    return ctx


def build_title(event: WatchEvent, options: ContentOptions, *, strict: bool = False) -> str:
    """Render the notification title for this event.

    Uses `options.title_template` if set; otherwise the per-event-type default
    from `DEFAULT_TITLE_TEMPLATES`. When `strict=True`, template errors
    (syntax or undefined variable) propagate — use only for the preview
    endpoint; the dispatcher path must call with the default `strict=False`.
    """
    tmpl = options.title_template or DEFAULT_TITLE_TEMPLATES[event.event_type.value]
    render = render_template_strict if strict else render_template
    return render(tmpl, build_template_context(event))


def resolve_options(config: ContentConfig | None, event_type: str) -> ContentOptions:
    """Return the effective ContentOptions for this event type.

    Falls back to ContentOptions() (all defaults) when config is None.
    Uses per-event override if present, otherwise config.default.
    """
    if config is None:
        return ContentOptions()
    return config.overrides.get(event_type) or config.default


def build_body(
    event: WatchEvent,
    options: ContentOptions,
    *,
    strict: bool = False,
    diff: ChangeDiff | None = None,
) -> str:
    """Compose a notification body from the event and resolved options.

    Three code paths:
      1. `options.body_template` set → render the user template (toggles do
         not apply). The user's `diff_snippet_lines` cap is applied so a
         template referencing `{{ diff_snippet }}` honors the preference.
      2. event_type is change_detected → `_build_change_detected_body`
         composes the body in Python from the shared
         `CHANGE_DETECTED_HEADER_LINES` tuple and interleaves toggle-driven
         sections at the canonical layout positions (per-anchor list in
         `_build_change_detected_body`).
      3. any other event_type → render the entry from `DEFAULT_BODY_TEMPLATES`
         (a single Jinja line; toggles do not apply).

    `diff` is this event's `ChangeDiff` — loaded by the dispatcher, computed
    from canned text by the preview. `None` means none was computed, and no
    diff section renders.

    `strict=True` selects the StrictUndefined Jinja env so template errors
    propagate. Use only for the preview endpoint; the dispatcher path must
    call with the default `strict=False`. The change_detected default path
    uses pure Python so `strict` has no effect there.
    """
    render = render_template_strict if strict else render_template
    if options.body_template:
        ctx = build_template_context(event, diff=diff, diff_snippet_cap=options.diff_snippet_lines)
        return render(options.body_template, ctx)

    if event.event_type == WatchEventType.CHANGE_DETECTED:
        return _build_change_detected_body(event, options, diff=diff)
    return render(
        DEFAULT_BODY_TEMPLATES[event.event_type.value],
        build_template_context(event),
    )


# Option A's label (D6, #326): the spec that bound moved between the two
# revisions, so part of the difference may be the selector's, not the page's.
SPEC_CHANGED_NOTE = (
    "NOTE: the source spec changed — some of this difference may come from the new selector"
)


def _build_change_detected_body(
    event: WatchEvent, options: ContentOptions, *, diff: ChangeDiff | None
) -> str:
    """Compose the change_detected body: a Markdown bullet list, then the diff.

    Every fact is a list item. Dispatch moved to the Notifier service in #137,
    which renders the source Markdown through CommonMark (mistune) for
    HTML-native channels (Mailgun, SES, mailto). Under CommonMark a lone `\\n`
    is a *soft* break (a space), so a paragraph of `\\n`-joined fact lines
    collapses onto one run-on line in HTML email (the #224 regression). A bullet
    list is real block structure — one `<li>` per fact — with no reliance on
    fragile trailing-whitespace hard breaks (#225).

    Header lines come from the canonical `CHANGE_DETECTED_HEADER_LINES` tuple in
    default_templates.py — same source of truth as the seed template returned by
    `compose_body_prefill`. The old `event_label` / `change_summary` body block
    was retired in #221 (see default_templates.py).

    Fact order (canonical); `?` items are toggle- and metadata-gated:
      item_name, DOMAIN?, URL, LAST CHANGED?, INTERVAL?, TIMESTAMP, ITEM,
      NOTE?, DESCRIPTION?, TAGS?, DIFF?

    NOTE is metadata-gated only (``extraction_changed == "spec"``, #326): it
    qualifies the change itself, so no toggle hides it. DIFF is the one-line
    "unavailable (<reason>)" when a requested diff could not be made (#222).

    The diff itself follows the list as a fenced ```diff block, separated by a
    blank line: its own block, so it neither breaks the list nor soft-wraps.

    Insertion anchors:
      - DOMAIN: after item_name
      - LAST CHANGED, INTERVAL: before TIMESTAMP (in that order)
      - DESCRIPTION, TAGS: appended after the header (trailing list items)
    """
    ctx = build_template_context(event)
    metadata = event.metadata

    # No base, no ITEM line (#296 D6): a relative link is useless in Slack or
    # email. Skipped by identity rather than by rendered prefix, and it is the
    # last header line, so the index anchors below are unaffected.
    items = [
        render_template(line, ctx)
        for line in CHANGE_DETECTED_HEADER_LINES
        if ctx["app_url"] or line != CHANGE_DETECTED_ITEM_LINE
    ]
    if options.include_domain and metadata.get("domain_name"):
        items.insert(1, f"DOMAIN: {metadata['domain_name']}")

    try:
        timestamp_idx = next(i for i, line in enumerate(items) if line.startswith("TIMESTAMP:"))
    except StopIteration as exc:
        raise RuntimeError(
            "CHANGE_DETECTED_HEADER_LINES missing TIMESTAMP — composer requires "
            "this anchor for LAST CHANGED / INTERVAL insertion"
        ) from exc
    pre_timestamp: list[str] = []
    if options.include_last_changed_at and metadata.get("last_changed_at"):
        pre_timestamp.append(f"LAST CHANGED: {metadata['last_changed_at']}")
    if options.include_temporal_context and metadata.get("check_interval"):
        pre_timestamp.append(f"INTERVAL: {metadata['check_interval']}")
    for offset, line in enumerate(pre_timestamp):
        items.insert(timestamp_idx + offset, line)

    if metadata.get("extraction_changed") == "spec":
        items.append(SPEC_CHANGED_NOTE)
    if options.include_description and metadata.get("description"):
        items.append(f"DESCRIPTION: {metadata['description']}")
    if options.include_tags and metadata.get("tags"):
        items.append(f"TAGS: {', '.join(metadata['tags'])}")
    wants_diff = options.include_diff_snippet or options.include_diff_full
    if wants_diff and diff is not None and diff.unavailable:
        items.append(f"DIFF: unavailable ({diff.unavailable})")

    listing = "\n".join(f"- {item}" for item in items)
    diff_text = _build_diff_text(diff, options)
    return f"{listing}\n\n{diff_text}" if diff_text else listing


def _build_diff_text(diff: ChangeDiff | None, options: ContentOptions) -> str:
    """Render the diff block respecting the snippet/full toggles.

    Returns empty string when both diff toggles are off, or when there is no
    diff to show (none computed, unavailable, or empty).
    """
    if not (options.include_diff_snippet or options.include_diff_full):
        return ""
    if diff is None or diff.unavailable or not diff.unified:
        return ""
    cap = None if options.include_diff_full else options.diff_snippet_lines
    return _render_unified_diff_block(diff.unified, max_lines=cap)


def _render_diff_variable(diff: ChangeDiff | None, *, max_lines: int | None) -> str:
    """One diff template variable: the fenced block, the reason, or empty."""
    if diff is None:
        return ""
    if diff.unavailable:
        return f"(diff unavailable: {diff.unavailable})"
    return _render_unified_diff_block(diff.unified, max_lines=max_lines)


def _normalize_unified_diff_lines(unified_diff: str) -> list[str]:
    """Split unified-diff text into non-empty lines.

    Real unified-diff output has no empty lines (content lines always carry a
    leading ` `, `+`, or `-` prefix), so dropping them is safe. The diff this
    module is fed (`diff.compute_unified_diff`) has none; the drop guards a
    trailing newline from becoming an empty line inside the fence.
    """
    return [line for line in unified_diff.split("\n") if line]


def _render_unified_diff_block(unified_diff: str | None, *, max_lines: int | None) -> str:
    """Wrap a unified-diff text in a Markdown ```diff fenced block.

    `max_lines=None` means no cap; the entire diff is rendered.
    A positive int caps the number of diff lines included; truncation is
    hunk-boundary aware (`@@ ...` lines mark hunk starts), and a `...
    (N more lines)` footer is appended inside the fence when truncated.

    Returns empty string when `unified_diff` is None or empty.
    """
    if not unified_diff:
        return ""
    lines = _normalize_unified_diff_lines(unified_diff)
    if not lines:
        return ""
    if max_lines is None:
        kept, omitted = lines, 0
    else:
        kept, omitted = _truncate_unified_diff_lines(lines, max_lines)
    body = "\n".join(kept)
    fenced = "```diff\n" + body + "\n"
    if omitted > 0:
        fenced += f"... ({omitted} more line{'s' if omitted != 1 else ''})\n"
    fenced += "```"
    return fenced


def _truncate_unified_diff_lines(lines: list[str], max_lines: int) -> tuple[list[str], int]:
    """Truncate diff lines to at most `max_lines` on a hunk boundary.

    The two file-header lines (`---` / `+++`) are always preserved when
    present. Each hunk is included whole or not at all — never truncated
    mid-hunk — except when even the first hunk doesn't fit, in which case
    only the file header + the first `@@` header line is included so the
    user can at least see where the diff begins.

    Returns `(kept_lines, omitted_line_count)`. `omitted_line_count == 0`
    means no truncation occurred.
    """
    if len(lines) <= max_lines:
        return lines, 0

    header_end = 0
    if len(lines) >= 2 and lines[0].startswith("---") and lines[1].startswith("+++"):
        header_end = 2

    hunk_starts = [i for i, line in enumerate(lines) if line.startswith("@@") and i >= header_end]
    if not hunk_starts:
        # No hunks; just truncate at line boundary, reserving room for footer.
        end = max(0, max_lines - 1)
        return lines[:end], len(lines) - end

    hunk_starts.append(len(lines))  # sentinel
    budget = max_lines - 1  # reserve one line for the footer
    end = header_end
    for i in range(len(hunk_starts) - 1):
        next_end = hunk_starts[i + 1]
        if next_end <= budget:
            end = next_end
        else:
            break
    if end <= header_end:
        # Even the first hunk doesn't fit; include header + the first @@
        # header line so the user at least sees where the diff starts.
        end = min(hunk_starts[0] + 1, budget)
        end = max(end, header_end)
    return lines[:end], len(lines) - end


def _dashboard_base() -> str:
    """The configured public base, or "" — never raises (#296 D6).

    The lifespan refuses to start on a malformed ``WATCHER_PUBLIC_BASE_URL``, so
    the service never gets here with one. A dispatch path must still not fail on
    it: a notification without its dashboard link beats no notification.
    """
    try:
        return public_base_url(os.environ) or ""
    except PublicBaseUrlInvalid:
        return ""


def _format_change_url(watched_item_id: str, change_revision_id: str | None, base: str) -> str:
    """Build the WatchedItem dashboard URL for a change, or "" when not a change event.

    #191: there is no per-change page (the `/watches/{id}/changes/...` route was
    retired with the Watch entity), so the link points at the WatchedItem detail
    page. Gated on `change_revision_id` so only change events surface a URL.

    Used by `build_template_context` to expose the URL as the `change_url`
    template variable, and by `_build_change_detected_body` for the CHANGE: line.
    """
    if not change_revision_id or not base:
        return ""
    return f"{base}/watched-items/{watched_item_id}"
