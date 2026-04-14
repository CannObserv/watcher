"""Notification body builder — resolves ContentOptions and composes custom bodies."""

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEvent


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

    The existing event.body is always the first section. Extra sections are
    appended based on options and available metadata keys. Sections are
    joined with a blank line.
    """
    parts = [event.body]

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
