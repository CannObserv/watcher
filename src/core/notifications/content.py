"""Notification body builder — resolves ContentOptions and composes custom bodies."""

from datetime import UTC, datetime

from jinja2 import Environment, StrictUndefined, TemplateError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.constants import APP_URL
from src.core.notifications.default_templates import (
    CHANGE_DETECTED_BODY_BLOCK_LINES,
    CHANGE_DETECTED_HEADER_LINES,
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType

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


def _compute_change_summary(event: WatchEvent) -> str:
    """Return '<N added, M modified, K removed>' for change_detected events.

    Empty string for other event types; 'details pending' for change_detected
    with no item metadata.
    """
    if event.event_type != WatchEventType.CHANGE_DETECTED:
        return ""
    parts: list[str] = []
    for label in ("added", "modified", "removed"):
        items = event.metadata.get(label, [])
        if items:
            parts.append(f"{len(items)} {label}")
    return ", ".join(parts) if parts else "details pending"


def _format_occurred_at_iso(dt: datetime) -> str:
    """Format an event timestamp as ISO 8601 with `Z` suffix (AGENTS.md format).

    Coerces to UTC (treating naive datetimes as UTC) so the output always
    carries a `Z` suffix and accurately reflects UTC, even if a producer
    accidentally passed a non-UTC tz-aware datetime.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_template_context(
    event: WatchEvent,
    *,
    diff_snippet_cap: int = _DEFAULT_DIFF_SNIPPET_CAP,
    unified_diff: str | None = None,
) -> dict:
    """Build Jinja2 template context from a WatchEvent.

    Includes metadata keys flattened in, plus derived fields that the default
    templates rely on:
      - `event_label` — human-readable event title (always set)
      - `occurred_at_iso` — ISO 8601 UTC timestamp (`...Z`), AGENTS.md format
      - `change_summary` — counts string for change_detected; empty otherwise
      - `change_url` — dashboard URL when `change_id` is in metadata; empty otherwise
      - `diff_snippet` — Markdown ```diff fenced unified diff, capped at
        `diff_snippet_cap` lines (hunk-boundary aware); empty when no
        `unified_diff` is provided.
      - `diff_full` — Markdown ```diff fenced unified diff, uncapped; empty
        when no `unified_diff` is provided.
      - `chunks_changed` — structured list of chunk-level changes:
        `[{"status": "added"|"removed"|"modified", "label": str,
        "similarity": float|None}, ...]`. Empty list when no chunk metadata.

    The dispatcher passes `unified_diff` after lazy-loading prev/curr extracted
    text from the Change row; the preview path computes it from canned text
    (see `preview_fixtures.compute_preview_unified_diff`). Both paths leave
    `diff_snippet`/`diff_full` empty when no diff is available.

    Derived fields are written *after* `metadata.update()` so that an event
    metadata dict that happens to share a key cannot clobber the value the
    template builder computed.
    """
    ctx = {
        "watch_id": event.watch_id,
        "watch_name": event.watch_name,
        "watch_url": event.watch_url,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
    }
    ctx.update(event.metadata)
    # Derived fields take precedence over any same-named metadata keys.
    ctx["event_label"] = EVENT_TITLES[event.event_type.value]
    ctx["occurred_at_iso"] = _format_occurred_at_iso(event.occurred_at)
    ctx["change_summary"] = _compute_change_summary(event)
    ctx["change_url"] = _format_change_url(event.watch_id, event.metadata.get("change_id"))
    ctx["diff_snippet"] = _render_unified_diff_block(unified_diff, max_lines=diff_snippet_cap)
    ctx["diff_full"] = _render_unified_diff_block(unified_diff, max_lines=None)
    ctx["chunks_changed"] = _format_chunks_changed(event.metadata)
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
    unified_diff: str | None = None,
) -> str:
    """Compose a notification body from the event and resolved options.

    Three code paths:
      1. `options.body_template` set → render the user template (toggles do
         not apply). The user's `diff_snippet_lines` cap is applied so a
         template referencing `{{ diff_snippet }}` honors the preference.
      2. event_type is change_detected → `_build_change_detected_body`
         composes the body in Python from the shared
         `CHANGE_DETECTED_HEADER_LINES` / `CHANGE_DETECTED_BODY_BLOCK_LINES`
         tuples and interleaves toggle-driven sections at the issue #104
         positions.
      3. any other event_type → render the entry from `DEFAULT_BODY_TEMPLATES`
         (a single Jinja line; toggles do not apply).

    `unified_diff` is the precomputed unified-diff text for this event (loaded
    by the dispatcher, computed from canned text by the preview path). Pass
    `None` when there is no diff data — `diff_snippet`/`diff_full` will be
    empty and the change_detected body's diff section will be omitted.

    `strict=True` selects the StrictUndefined Jinja env so template errors
    propagate. Use only for the preview endpoint; the dispatcher path must
    call with the default `strict=False`. The change_detected default path
    uses pure Python so `strict` has no effect there.
    """
    render = render_template_strict if strict else render_template
    if options.body_template:
        # Pass the user's diff_snippet_lines cap through so a custom template
        # referencing {{ diff_snippet }} honors the preference rather than
        # silently using the module default.
        ctx = build_template_context(
            event,
            diff_snippet_cap=options.diff_snippet_lines,
            unified_diff=unified_diff,
        )
        return render(options.body_template, ctx)

    if event.event_type == WatchEventType.CHANGE_DETECTED:
        return _build_change_detected_body(event, options, unified_diff=unified_diff)
    return render(
        DEFAULT_BODY_TEMPLATES[event.event_type.value],
        build_template_context(event, unified_diff=unified_diff),
    )


def _build_change_detected_body(
    event: WatchEvent, options: ContentOptions, *, unified_diff: str | None
) -> str:
    """Compose the change_detected body following the issue #104 layout.

    Header and body-block lines come from the canonical
    `CHANGE_DETECTED_HEADER_LINES` / `CHANGE_DETECTED_BODY_BLOCK_LINES`
    tuples in default_templates.py — same source of truth as the seed
    template returned by `compose_body_prefill`.

    Toggle-driven section anchors:
      - DOMAIN: header.insert(1, …)  (after watch_name)
      - CHANGE: header.append(…)     (after WATCH, the last header line)
      - diff: own paragraph after the body block
      - INTERVAL / LAST CHANGED / SIGNIFICANCE: grouped in one stats paragraph
      - DESCRIPTION / TAGS: each its own paragraph, in that order

    Reordering the canonical tuples requires updating the insert/append
    indices here.
    """
    ctx = build_template_context(event, unified_diff=unified_diff)
    metadata = event.metadata

    header = [render_template(line, ctx) for line in CHANGE_DETECTED_HEADER_LINES]
    if options.include_domain and metadata.get("effective_domain"):
        header.insert(1, f"DOMAIN: {metadata['effective_domain']}")
    if options.include_change_dashboard_url and metadata.get("change_id"):
        header.append(f"CHANGE: {ctx['change_url']}")

    body_block = [render_template(line, ctx) for line in CHANGE_DETECTED_BODY_BLOCK_LINES]

    paragraphs: list[list[str]] = [header, body_block]

    diff_text = _build_diff_text(unified_diff, options)
    if diff_text:
        paragraphs.append(diff_text.splitlines())

    stats: list[str] = []
    if options.include_temporal_context and metadata.get("check_interval"):
        stats.append(f"INTERVAL: {metadata['check_interval']}")
    if options.include_last_changed_at and metadata.get("last_changed_at"):
        stats.append(f"LAST CHANGED: {metadata['last_changed_at']}")
    if options.include_significance and metadata.get("significance") is not None:
        stats.append(f"SIGNIFICANCE: {int(metadata['significance'] * 100)}%")
    if stats:
        paragraphs.append(stats)

    if options.include_description and metadata.get("description"):
        paragraphs.append([f"DESCRIPTION: {metadata['description']}"])
    if options.include_tags and metadata.get("tags"):
        paragraphs.append([f"TAGS: {', '.join(metadata['tags'])}"])

    return "\n\n".join("\n".join(p) for p in paragraphs)


def _build_diff_text(unified_diff: str | None, options: ContentOptions) -> str:
    """Render the diff block respecting the snippet/full toggles.

    Returns empty string when both diff toggles are off, when `unified_diff`
    is missing, or when it has no content.
    """
    if not (options.include_diff_snippet or options.include_diff_full):
        return ""
    if not unified_diff:
        return ""
    cap = None if options.include_diff_full else options.diff_snippet_lines
    return _render_unified_diff_block(unified_diff, max_lines=cap)


def _normalize_unified_diff_lines(unified_diff: str) -> list[str]:
    """Split unified-diff text into non-empty lines.

    `compute_unified_diff` emits content lines with both their input `\n` and
    the joining `\n`, producing blank lines between every content line.
    Real unified-diff output has no empty lines (content lines always carry
    a leading ` `, `+`, or `-` prefix), so dropping empty lines is safe and
    yields the canonical diff layout users expect inside the Markdown fence.
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


def _format_chunks_changed(metadata: dict) -> list[dict]:
    """Return structured chunk-level changes.

    Each entry is a dict with keys:
      - status: "added" | "removed" | "modified"
      - label: chunk label (str)
      - similarity: float | None  (only meaningful for modified)

    Returns an empty list when no chunk metadata is present. This is the
    structured replacement for the old chunk-label summary string — custom
    templates that want a textual rendering can iterate over it.
    """
    out: list[dict] = []
    for label in metadata.get("added", []) or []:
        out.append({"status": "added", "label": label, "similarity": None})
    for label in metadata.get("removed", []) or []:
        out.append({"status": "removed", "label": label, "similarity": None})
    for item in metadata.get("modified", []) or []:
        label = item.get("label")
        if not label:
            continue
        out.append(
            {
                "status": "modified",
                "label": label,
                "similarity": item.get("similarity"),
            }
        )
    return out


def _format_change_url(watch_id: str, change_id: str | None) -> str:
    """Build the dashboard URL for a specific change, or empty string if no change_id.

    Used by `build_template_context` to expose the URL as the `change_url`
    template variable, and by `_build_change_detected_body` for the CHANGE: line.
    """
    if not change_id:
        return ""
    return f"{APP_URL}/watches/{watch_id}/changes/{change_id}"
