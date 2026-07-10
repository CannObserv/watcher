"""Notification body builder — resolves ContentOptions and composes custom bodies."""

from jinja2 import Environment, StrictUndefined, TemplateError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.constants import APP_URL
from src.core.notifications.default_templates import (
    CHANGE_DETECTED_HEADER_LINES,
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.utils import format_utc_iso

_jinja_env = Environment(autoescape=False)
_jinja_env_strict = Environment(autoescape=False, undefined=StrictUndefined)


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


def build_template_context(event: WatchEvent) -> dict:
    """Build Jinja2 template context from a WatchEvent.

    Includes metadata keys flattened in, plus derived fields that the default
    templates rely on:
      - `event_label` — human-readable event title (always set)
      - `occurred_at_iso` — ISO 8601 UTC timestamp (`...Z`), AGENTS.md format
      - `change_url` — WatchedItem dashboard URL when `change_revision_id` is in
        metadata; empty otherwise

    The diff-derived fields (`change_summary`, `diff_snippet`, `diff_full`,
    `chunks_changed`) were removed in #221 — the diff pipeline that fed them was
    dropped in Phase 5 (#156). Diff restoration is tracked in #222.

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
    ctx["change_url"] = _format_change_url(
        event.watched_item_id, event.metadata.get("change_revision_id")
    )
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
) -> str:
    """Compose a notification body from the event and resolved options.

    Three code paths:
      1. `options.body_template` set → render the user template (toggles do
         not apply).
      2. event_type is change_detected → `_build_change_detected_body`
         composes the body in Python from the shared
         `CHANGE_DETECTED_HEADER_LINES` tuple and interleaves toggle-driven
         sections at the canonical layout positions (per-anchor list in
         `_build_change_detected_body`).
      3. any other event_type → render the entry from `DEFAULT_BODY_TEMPLATES`
         (a single Jinja line; toggles do not apply).

    `strict=True` selects the StrictUndefined Jinja env so template errors
    propagate. Use only for the preview endpoint; the dispatcher path must
    call with the default `strict=False`. The change_detected default path
    uses pure Python so `strict` has no effect there.
    """
    render = render_template_strict if strict else render_template
    if options.body_template:
        return render(options.body_template, build_template_context(event))

    if event.event_type == WatchEventType.CHANGE_DETECTED:
        return _build_change_detected_body(event, options)
    return render(
        DEFAULT_BODY_TEMPLATES[event.event_type.value],
        build_template_context(event),
    )


def _build_change_detected_body(event: WatchEvent, options: ContentOptions) -> str:
    """Compose the change_detected body.

    Header lines come from the canonical `CHANGE_DETECTED_HEADER_LINES` tuple in
    default_templates.py — same source of truth as the seed template returned by
    `compose_body_prefill`. The body is the header alone plus optional
    toggle-driven sections; the old `event_label` / `change_summary` body block
    was retired in #221 (see default_templates.py).

    Toggle-driven section anchors (header):
      - DOMAIN: after item_name
      - LAST CHANGED, INTERVAL: before TIMESTAMP (in that order)

    Trailing paragraphs (each its own):
      - DESCRIPTION
      - TAGS
    """
    ctx = build_template_context(event)
    metadata = event.metadata

    header = [render_template(line, ctx) for line in CHANGE_DETECTED_HEADER_LINES]
    if options.include_domain and metadata.get("domain_name"):
        header.insert(1, f"DOMAIN: {metadata['domain_name']}")

    try:
        timestamp_idx = next(i for i, line in enumerate(header) if line.startswith("TIMESTAMP:"))
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
        header.insert(timestamp_idx + offset, line)

    paragraphs: list[list[str]] = [header]

    if options.include_description and metadata.get("description"):
        paragraphs.append([f"DESCRIPTION: {metadata['description']}"])
    if options.include_tags and metadata.get("tags"):
        paragraphs.append([f"TAGS: {', '.join(metadata['tags'])}"])

    return "\n\n".join("\n".join(p) for p in paragraphs)


def _format_change_url(watched_item_id: str, change_revision_id: str | None) -> str:
    """Build the WatchedItem dashboard URL for a change, or "" when not a change event.

    #191: there is no per-change page (the `/watches/{id}/changes/...` route was
    retired with the Watch entity), so the link points at the WatchedItem detail
    page. Gated on `change_revision_id` so only change events surface a URL.

    Used by `build_template_context` to expose the URL as the `change_url`
    template variable, and by `_build_change_detected_body` for the CHANGE: line.
    """
    if not change_revision_id:
        return ""
    return f"{APP_URL}/watched-items/{watched_item_id}"
