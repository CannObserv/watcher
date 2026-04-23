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
# rather than a wall of entries. Use `{{ diff_full }}` for the unbounded version.
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


def build_template_context(event: WatchEvent) -> dict:
    """Build Jinja2 template context from a WatchEvent.

    Includes metadata keys flattened in, plus derived fields that the default
    templates rely on:
      - `event_label` — human-readable event title (always set)
      - `occurred_at_iso` — ISO 8601 UTC timestamp (`...Z`), AGENTS.md format
      - `change_summary` — counts string for change_detected; empty otherwise
      - `change_url` — dashboard URL when `change_id` is in metadata; empty otherwise
      - `diff_snippet` — pre-rendered diff lines capped at the same default as
        `ContentOptions.diff_snippet_lines`; empty when no diff data
      - `diff_full` — pre-rendered diff lines, uncapped; empty when no diff data

    User templates referencing any of these on events that don't populate them
    will render blank.

    Derived fields are written *after* `metadata.update()` so that an event
    metadata dict that happens to share a key (e.g., `change_url`) cannot
    clobber the value the template builder computed.
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
    ctx["diff_snippet"] = _render_diff_lines(event.metadata, max_entries=_DEFAULT_DIFF_SNIPPET_CAP)
    ctx["diff_full"] = _render_diff_lines(event.metadata, max_entries=None)
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


def build_body(event: WatchEvent, options: ContentOptions, *, strict: bool = False) -> str:
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

    `strict=True` selects the StrictUndefined Jinja env so template errors
    propagate. Use only for the preview endpoint; the dispatcher path must
    call with the default `strict=False`. The change_detected default path
    uses pure Python so `strict` has no effect there.
    """
    render = render_template_strict if strict else render_template
    if options.body_template:
        ctx = build_template_context(event)
        # User's diff_snippet_lines cap takes precedence over the module
        # default that build_template_context applies — otherwise the
        # preference would be silently ignored on the body_template path.
        ctx["diff_snippet"] = _render_diff_lines(
            event.metadata, max_entries=options.diff_snippet_lines
        )
        return render(options.body_template, ctx)

    if event.event_type == WatchEventType.CHANGE_DETECTED:
        return _build_change_detected_body(event, options)
    return render(DEFAULT_BODY_TEMPLATES[event.event_type.value], build_template_context(event))


def _build_change_detected_body(event: WatchEvent, options: ContentOptions) -> str:
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
    ctx = build_template_context(event)
    metadata = event.metadata

    header = [render_template(line, ctx) for line in CHANGE_DETECTED_HEADER_LINES]
    if options.include_domain and metadata.get("effective_domain"):
        header.insert(1, f"DOMAIN: {metadata['effective_domain']}")
    if options.include_change_dashboard_url and metadata.get("change_id"):
        header.append(f"CHANGE: {ctx['change_url']}")

    body_block = [render_template(line, ctx) for line in CHANGE_DETECTED_BODY_BLOCK_LINES]

    paragraphs: list[list[str]] = [header, body_block]

    diff_text = _build_diff_text(metadata, options)
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


def _build_diff_text(metadata: dict, options: ContentOptions) -> str:
    """Render the diff block respecting the snippet/full toggles.

    Returns empty string when both diff toggles are off or when there is no
    diff data at all.
    """
    if not (options.include_diff_snippet or options.include_diff_full):
        return ""
    cap = None if options.include_diff_full else options.diff_snippet_lines
    return _render_diff_lines(metadata, max_entries=cap)


def _render_diff_lines(metadata: dict, *, max_entries: int | None) -> str:
    """Format chunk-level change summary. Returns empty string if no diff data.

    `max_entries=None` means no cap (include every entry). A positive int caps
    the total number of lines to include. Used by `_build_diff_text` (with
    options.diff_snippet_lines) and `build_template_context` (to expose the
    rendered text as `diff_snippet` / `diff_full` template variables).
    """
    added = metadata.get("added", [])
    removed = metadata.get("removed", [])
    modified = metadata.get("modified", [])

    if not added and not removed and not modified:
        return ""

    entries: list[str] = []
    for label in added:
        entries.append(f"  + {label}")
    for label in removed:
        entries.append(f"  - {label}")
    for item in modified:
        label = item.get("label")
        if not label:
            continue
        similarity = item.get("similarity")
        if similarity is None:
            entries.append(f"  ~ {label}")
        else:
            entries.append(f"  ~ {label} ({int(similarity * 100)}% similar)")

    if max_entries is not None:
        entries = entries[:max_entries]

    return "Changed sections:\n" + "\n".join(entries)


def _format_change_url(watch_id: str, change_id: str | None) -> str:
    """Build the dashboard URL for a specific change, or empty string if no change_id.

    Used by `build_template_context` to expose the URL as the `change_url`
    template variable, and by `_build_change_detected_body` for the CHANGE: line.
    """
    if not change_id:
        return ""
    return f"{APP_URL}/watches/{watch_id}/changes/{change_id}"
