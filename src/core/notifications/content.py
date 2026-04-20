"""Notification body builder — resolves ContentOptions and composes custom bodies."""

from jinja2 import Environment, TemplateError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.constants import APP_URL
from src.core.notifications.default_templates import (
    DEFAULT_BODY_TEMPLATES,
    DEFAULT_TITLE_TEMPLATES,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType

_jinja_env = Environment(autoescape=False)


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
    """Render a Jinja2 template string, raising on error.

    Use only in contexts where the user expects to see template errors —
    e.g. the preview endpoint. Dispatch uses `render_template` so a bad
    template never breaks a real notification.
    """
    tmpl = _jinja_env.from_string(template_str)
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


def build_template_context(event: WatchEvent) -> dict:
    """Build Jinja2 template context from a WatchEvent.

    Includes metadata keys flattened in, plus derived fields (`event_label`,
    `change_summary`) that the default templates rely on.
    """
    ctx = {
        "watch_id": event.watch_id,
        "watch_name": event.watch_name,
        "watch_url": event.watch_url,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "event_label": EVENT_TITLES[event.event_type.value],
        "change_summary": _compute_change_summary(event),
    }
    ctx.update(event.metadata)
    return ctx


def build_title(event: WatchEvent, options: ContentOptions) -> str:
    """Render the notification title for this event.

    Uses `options.title_template` if set; otherwise the per-event-type default
    from `DEFAULT_TITLE_TEMPLATES`.
    """
    tmpl = options.title_template or DEFAULT_TITLE_TEMPLATES[event.event_type.value]
    return render_template(tmpl, build_template_context(event))


def resolve_options(config: ContentConfig | None, event_type: str) -> ContentOptions:
    """Return the effective ContentOptions for this event type.

    Falls back to ContentOptions() (all defaults) when config is None.
    Uses per-event override if present, otherwise config.default.
    """
    if config is None:
        return ContentOptions()
    return config.overrides.get(event_type) or config.default


def build_body(event: WatchEvent, options: ContentOptions) -> str:
    """Compose a notification body from the event and resolved options.

    If options.body_template is set, render it as a Jinja2 template and return
    immediately (no additive sections). Otherwise, the default body for this
    event type is rendered from DEFAULT_BODY_TEMPLATES and extra sections are
    appended based on toggle options. Sections are joined with a blank line.
    """
    if options.body_template:
        return render_template(options.body_template, build_template_context(event))

    default_body = render_template(
        DEFAULT_BODY_TEMPLATES[event.event_type.value], build_template_context(event)
    )
    parts = [default_body]

    diff_section = _build_diff_section(event.metadata, options)
    if diff_section:
        parts.append(diff_section)

    if options.include_temporal_context:
        temporal = _build_temporal_section(event.metadata)
        if temporal:
            parts.append(temporal)

    if options.include_domain:
        domain = _build_domain_section(event.metadata)
        if domain:
            parts.append(domain)

    if options.include_last_changed_at:
        last_changed = _build_last_changed_section(event.metadata)
        if last_changed:
            parts.append(last_changed)

    if options.include_significance:
        sig = _build_significance_section(event.metadata)
        if sig:
            parts.append(sig)

    if options.include_change_dashboard_url:
        url_section = _build_change_url_section(event.watch_id, event.metadata)
        if url_section:
            parts.append(url_section)

    if options.include_tags:
        tags_section = _build_tags_section(event.metadata)
        if tags_section:
            parts.append(tags_section)

    if options.include_description:
        desc_section = _build_description_section(event.metadata)
        if desc_section:
            parts.append(desc_section)

    return "\n\n".join(parts)


def _build_diff_section(metadata: dict, options: ContentOptions) -> str:
    """Format chunk-level change summary. Returns empty string if no diff data."""
    if not (options.include_diff_snippet or options.include_diff_full):
        return ""

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
        pct = int(item["similarity"] * 100)
        entries.append(f"  ~ {item['label']} ({pct}% similar)")

    # Snippet mode: limit total entries; full mode: no limit (include_diff_full supersedes)
    if not options.include_diff_full:
        entries = entries[: options.diff_snippet_lines]

    return "Changed sections:\n" + "\n".join(entries)


def _build_temporal_section(metadata: dict) -> str:
    """Format check interval. Returns empty string if not in metadata."""
    interval = metadata.get("check_interval")
    if not interval:
        return ""
    return f"Check interval: {interval}"


def _build_domain_section(metadata: dict) -> str:
    """Format effective domain. Returns empty string if not in metadata."""
    domain = metadata.get("effective_domain")
    if not domain:
        return ""
    return f"Domain: {domain}"


def _build_last_changed_section(metadata: dict) -> str:
    """Format last changed date. Returns empty string if not in metadata."""
    date = metadata.get("last_changed_at")
    if not date:
        return ""
    return f"Last changed: {date}"


def _build_significance_section(metadata: dict) -> str:
    """Format change significance percentage. Returns empty string if not in metadata."""
    sig = metadata.get("significance")
    if sig is None:
        return ""
    return f"Significance: {int(sig * 100)}%"


def _build_change_url_section(watch_id: str, metadata: dict) -> str:
    """Format dashboard URL for a change. Returns empty string if change_id absent."""
    change_id = metadata.get("change_id")
    if not change_id:
        return ""
    return f"View change: {APP_URL}/watches/{watch_id}/changes/{change_id}"


def _build_tags_section(metadata: dict) -> str:
    """Format tags list. Returns empty string if not in metadata or empty."""
    tags = metadata.get("tags")
    if not tags:
        return ""
    return "Tags: " + ", ".join(tags)


def _build_description_section(metadata: dict) -> str:
    """Format watch description. Returns empty string if not in metadata."""
    description = metadata.get("description")
    if not description:
        return ""
    return f"Description: {description}"
