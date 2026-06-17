"""Default Jinja2 templates for notification titles and bodies.

Single source of truth for the built-in, per-event-type defaults.
User-supplied `title_template` / `body_template` values from ContentOptions
override them. Keeping defaults here (not as Python code on WatchEvent)
means the UI can display and pre-fill them exactly as they'll render.

For most events the dispatcher renders `DEFAULT_BODY_TEMPLATES[event_type]`
directly through Jinja. The `change_detected` body is the exception:
`src.core.notifications.content.build_body` composes it line-by-line in
Python from the shared `CHANGE_DETECTED_HEADER_LINES` and
`CHANGE_DETECTED_BODY_BLOCK_LINES` tuples (single source of truth) and
interleaves optional toggle-driven sections (DOMAIN, LAST CHANGED, INTERVAL,
CHANGE, SIGNIFICANCE in the header; diff, DESCRIPTION, TAGS as trailing
paragraphs) at the issue-#155 positions.
`DEFAULT_BODY_TEMPLATES['change_detected']` is derived from the same tuples
and serves only as the UI seed (`compose_body_prefill`).

Template context (shared with user templates) is built by
`src.core.notifications.content.build_template_context`.
"""

from dataclasses import dataclass

from src.core.notifications.constants import APP_URL
from src.core.notifications.events import WatchEventType


@dataclass(frozen=True, slots=True)
class TemplateVariable:
    """Metadata about a Jinja variable available to user notification templates.

    Drives the variable chip row + reference drawer in the dashboard UI.
    """

    name: str
    type: str
    description: str
    scope: str  # "always" | "change_detected" | "watch_error" | "contextual"


TEMPLATE_VARIABLES: list[TemplateVariable] = [
    # Always available
    TemplateVariable("watched_item_id", "str", "ULID of the watched item", "always"),
    TemplateVariable("item_name", "str", "Display name of the watched item", "always"),
    TemplateVariable("item_url", "str", "Monitored URL", "always"),
    TemplateVariable("event_type", "str", 'Event code (e.g. "change_detected")', "always"),
    TemplateVariable("event_label", "str", 'Human label (e.g. "Change Detected")', "always"),
    TemplateVariable("occurred_at", "datetime", "When the event occurred (UTC)", "always"),
    TemplateVariable(
        "occurred_at_iso",
        "str",
        "ISO 8601 UTC timestamp (e.g. `2026-04-23T00:38:33Z`)",
        "always",
    ),
    # change_detected-only
    TemplateVariable(
        "change_summary",
        "str",
        'Counts string (e.g. "2 added, 1 modified")',
        "change_detected",
    ),
    TemplateVariable("added", "list[str]", "Labels of added sections", "change_detected"),
    TemplateVariable(
        "modified",
        "list[{label, similarity}]",
        "Modified sections with similarity scores",
        "change_detected",
    ),
    TemplateVariable("removed", "list[str]", "Labels of removed sections", "change_detected"),
    TemplateVariable(
        "diff_snippet",
        "str",
        "Unified diff in a Markdown ```diff block, capped (hunk-boundary aware)",
        "change_detected",
    ),
    TemplateVariable(
        "diff_full",
        "str",
        "Unified diff in a Markdown ```diff block, no cap",
        "change_detected",
    ),
    TemplateVariable(
        "chunks_changed",
        "list[{status, label, similarity}]",
        "Structured list of chunk-level changes (status: added/removed/modified)",
        "change_detected",
    ),
    TemplateVariable("change_id", "str", "ULID of the change for URLs", "change_detected"),
    TemplateVariable(
        "change_url", "str", "Direct dashboard URL for this change", "change_detected"
    ),
    TemplateVariable("significance", "float", "Change magnitude 0.0–1.0", "change_detected"),
    # watch_error-only
    TemplateVariable("status_code", "int", "HTTP status code returned", "watch_error"),
    # Contextual — populated when relevant metadata exists on the watch
    TemplateVariable("domain_name", "str", "Resolved domain of the watch URL", "contextual"),
    TemplateVariable("check_interval", "str", 'Check cadence (e.g. "1h")', "contextual"),
    TemplateVariable(
        "last_changed_at", "str", "UTC timestamp of last detected change", "contextual"
    ),
    TemplateVariable("tags", "list[str]", "Watch tags", "contextual"),
    TemplateVariable("description", "str", "Watch description", "contextual"),
]
"""Authoritative list of variables usable in user title/body templates.

Consumed by `partials/notification_variable_chips.html` (chip row) and the
expandable [See all variables] reference drawer. Keep in sync with
`src.core.notifications.content.build_template_context`.
"""

_TITLE = "[Watcher] {{ event_label }}: {{ item_name }}"


DEFAULT_TITLE_TEMPLATES: dict[str, str] = {
    WatchEventType.CHANGE_DETECTED.value: _TITLE,
    WatchEventType.WATCH_ERROR.value: _TITLE,
    WatchEventType.WATCH_RECOVERED.value: _TITLE,
    WatchEventType.WATCH_CREATED.value: _TITLE,
    WatchEventType.WATCH_PAUSED.value: _TITLE,
    WatchEventType.WATCH_RESUMED.value: _TITLE,
    WatchEventType.WATCH_ARCHIVED.value: _TITLE,
    WatchEventType.WATCH_DELETED.value: _TITLE,
}


# Canonical skeleton for the change_detected default body. Both the seed
# template (DEFAULT_BODY_TEMPLATES['change_detected']) and the dispatch-time
# composer (`content.build_body`) consume these tuples — single source of
# truth for the always-present lines. Toggle-driven sections are interleaved
# by the composer at the issue #155 positions; the WATCH dashboard link is
# part of the unconditional skeleton.
#
# Composer insertion anchors in HEADER (see `_build_change_detected_body`):
#   - DOMAIN: immediately after item_name (index 1)
#   - LAST CHANGED, INTERVAL: immediately before TIMESTAMP (in that order)
#   - CHANGE: immediately after WATCH
#   - SIGNIFICANCE: immediately after CHANGE (or after WATCH when CHANGE off)
# Reorder these tuples and the composer's index/append calls must follow.
CHANGE_DETECTED_HEADER_LINES: tuple[str, ...] = (
    "{{ item_name }}",
    "URL: {{ item_url }}",
    "TIMESTAMP: {{ occurred_at_iso }}",
    f"WATCH: {APP_URL}" + "/watched-items/{{ watched_item_id }}",
)
CHANGE_DETECTED_BODY_BLOCK_LINES: tuple[str, ...] = (
    "{{ event_label }}",
    "{{ change_summary }}",
)

_CHANGE_DETECTED_BODY = (
    "\n".join(CHANGE_DETECTED_HEADER_LINES) + "\n\n" + "\n".join(CHANGE_DETECTED_BODY_BLOCK_LINES)
)


DEFAULT_BODY_TEMPLATES: dict[str, str] = {
    WatchEventType.CHANGE_DETECTED.value: _CHANGE_DETECTED_BODY,
    WatchEventType.WATCH_ERROR.value: (
        "{{ item_url }} returned HTTP {{ status_code | default('unknown') }}"
    ),
    WatchEventType.WATCH_RECOVERED.value: "{{ item_url }} is responding normally again",
    WatchEventType.WATCH_CREATED.value: "Now monitoring {{ item_url }}",
    WatchEventType.WATCH_PAUSED.value: "Watch paused: {{ item_url }}",
    WatchEventType.WATCH_RESUMED.value: "Watch resumed: {{ item_url }}",
    WatchEventType.WATCH_ARCHIVED.value: "Watch archived: {{ item_url }}",
    WatchEventType.WATCH_DELETED.value: "Watch deleted: {{ item_url }}",
}


def compose_title_prefill(event_type: str) -> str:
    """Return the default title Jinja template for this event type.

    Used to pre-fill the title textarea when a user first clicks [Edit template].
    """
    return DEFAULT_TITLE_TEMPLATES[event_type]


def compose_body_prefill(event_type: str) -> str:
    """Return the default body Jinja template for this event type.

    Used by the dashboard "Show default template" UX so the user can copy the
    skeleton into a custom body_template and edit from there. Toggle state has
    no effect — toggles drive Python-side interleaving in `build_body`, not
    the seed template.
    """
    return DEFAULT_BODY_TEMPLATES[event_type]
