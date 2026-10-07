"""Notification body builder — resolves ContentOptions and composes custom bodies."""

import os
import re

from jinja2 import Environment, StrictUndefined, TemplateError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.default_templates import (
    CHANGE_DETECTED_HEADER_LINES,
    CHANGE_DETECTED_ITEM_LINE,
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.diff import ChangeDiff, Hunk
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.public_base_url import PublicBaseUrlInvalid, public_base_url
from src.core.utils import format_utc_iso

_jinja_env = Environment(autoescape=False)
_jinja_env_strict = Environment(autoescape=False, undefined=StrictUndefined)

# Default cap for the `diff_snippet` template variable. Lifted from the
# Pydantic field default so the two stay in lockstep — when a custom user
# template references `{{ diff_snippet }}`, they get a sensibly-bounded slice
# rather than every change. Use `{{ diff_full }}` for all of them.
_DEFAULT_DIFF_SNIPPET_CAP: int = ContentOptions.model_fields["diff_snippet_lines"].default

#: The most a rendered diff body may be, footer included (#346, built in #349).
#: A word diff scales with the change, but a rewrite is a big change and one
#: huge token defeats a line cap; an oversized body the notifier or a channel
#: rejects would cost the recipient the whole notification. Under Slack's
#: 40 000-character message text limit and Gmail's 102 KB clip, with room for
#: the header and HTML escaping. Notifier's own request limit is not visible
#: from here; if it publishes one, this is the number to revisit.
MAX_RENDERED_DIFF_BYTES = 32 * 1024
# Room kept for the `... (N more lines)` footer and its newline.
_FOOTER_BYTES = 32

_BACKTICK_RUN = re.compile(r"`+")


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
      - `diff_snippet` — Markdown ```diff fenced change diff (#349: the
        changed words with context), capped at `diff_snippet_cap` lines
        (hunk-boundary aware)
      - `diff_full` — every change; only the byte backstop applies

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
      item_name, DOMAIN?, URL, PREVIOUS CHANGE?, INTERVAL?, TIMESTAMP, ITEM,
      NOTE?, DESCRIPTION?, TAGS?, DIFF?

    PREVIOUS CHANGE is the `include_last_changed_at` toggle on a change (#349):
    `last_changed_at` is *this* change by the time the event is built, so it
    only repeated TIMESTAMP; `previous_changed_at` is the change before it,
    absent on an item's first.

    NOTE is metadata-gated only (``extraction_changed == "spec"``, #326): it
    qualifies the change itself, so no toggle hides it. DIFF is the one-line
    "unavailable (<reason>)" when a requested diff could not be made (#222).

    The diff itself follows the list as a fenced ```diff block, separated by a
    blank line: its own block, so it neither breaks the list nor soft-wraps.

    Insertion anchors:
      - DOMAIN: after item_name
      - PREVIOUS CHANGE, INTERVAL: before TIMESTAMP (in that order)
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
            "this anchor for PREVIOUS CHANGE / INTERVAL insertion"
        ) from exc
    pre_timestamp: list[str] = []
    if options.include_last_changed_at and metadata.get("previous_changed_at"):
        pre_timestamp.append(f"PREVIOUS CHANGE: {metadata['previous_changed_at']}")
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
    if diff is None or diff.unavailable:
        return ""
    cap = None if options.include_diff_full else options.diff_snippet_lines
    return _render_diff_block(diff.hunks, max_lines=cap)


def _render_diff_variable(diff: ChangeDiff | None, *, max_lines: int | None) -> str:
    """One diff template variable: the fenced block, the reason, or empty."""
    if diff is None:
        return ""
    if diff.unavailable:
        return f"(diff unavailable: {diff.unavailable})"
    return _render_diff_block(diff.hunks, max_lines=max_lines)


def _render_diff_block(hunks: tuple[Hunk, ...], *, max_lines: int | None) -> str:
    """The hunks in a Markdown ```diff fenced block, an empty line between two.

    The fence is three backticks unless the content holds a run as long, in
    which case it is one longer (``_fence_for``).

    `max_lines=None` means every hunk; a positive int caps the rendered lines,
    cut on a hunk boundary (``_truncate_hunks``). Either way the block's body
    stays within ``MAX_RENDERED_DIFF_BYTES``. A `... (N more lines)` footer
    is appended inside the fence when anything was cut.

    Returns empty string when there are no hunks.
    """
    if not hunks:
        return ""
    kept, omitted = _truncate_hunks(hunks, max_lines=max_lines, max_bytes=MAX_RENDERED_DIFF_BYTES)
    if omitted:
        kept = [*kept, f"... ({omitted} more line{'s' if omitted != 1 else ''})"]
    body = "\n".join(kept)
    fence = _fence_for(body)
    return f"{fence}diff\n{body}\n{fence}"


def _fence_for(body: str) -> str:
    """A backtick fence no line of ``body`` can close (CR 13).

    The diff is the watched page's own text, and the notifier renders the body
    as CommonMark: a backtick fence closes on a line that is only a run at
    least as long (indented 0–3 spaces, trailing spaces allowed) — and a diff
    context line is the page's text behind two spaces, so a page line that is
    exactly three backticks would end the block and the page's next line would
    render as live Markdown (CR 14). One backtick longer than the longest run
    in the content (three at minimum) means no line can close it.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(body)), default=0)
    return "`" * max(3, longest + 1)


def _joined_size(lines: list[str]) -> int:
    return len("\n".join(lines).encode())


def _truncate_hunks(
    hunks: tuple[Hunk, ...], *, max_lines: int | None, max_bytes: int
) -> tuple[list[str], int]:
    """The hunks' lines, cut to fit, and how many lines were not shown in full.

    Hunks are separated by an empty line, which counts toward ``max_lines``
    but never as an omitted line. When everything fits it is returned whole.
    Otherwise one line and ``_FOOTER_BYTES`` are kept back for the footer, and
    whole hunks are kept while they fit. If not even the first does, its
    leading lines are kept instead (each is readable page text, #349), the
    last of them cut within the line when one alone outgrows the bytes.

    Returns `(kept_lines, omitted_line_count)`; `0` means nothing was cut.
    """
    every = [line for index, hunk in enumerate(hunks) for line in (*([""] if index else []), *hunk)]
    if (max_lines is None or len(every) <= max_lines) and _joined_size(every) <= max_bytes:
        return every, 0

    line_budget = None if max_lines is None else max_lines - 1
    byte_budget = max_bytes - _FOOTER_BYTES

    def fits(lines: list[str]) -> bool:
        within_lines = line_budget is None or len(lines) <= line_budget
        return within_lines and _joined_size(lines) <= byte_budget

    kept: list[str] = []
    for hunk in hunks:
        candidate = [*kept, *([""] if kept else []), *hunk]
        if not fits(candidate):
            break
        kept = candidate
    shown = sum(1 for line in kept if line)
    if not kept:
        for line in hunks[0]:
            if line_budget is not None and len(kept) >= line_budget:
                break
            if fits([*kept, line]):
                kept.append(line)
                shown += 1
                continue
            room = byte_budget - _joined_size([*kept, ""])
            if room > len("…".encode()):
                cut = line.encode()[: room - len("…".encode())].decode(errors="ignore")
                kept.append(f"{cut}…")
            break
    total = sum(len(hunk) for hunk in hunks)
    return kept, total - shown


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
