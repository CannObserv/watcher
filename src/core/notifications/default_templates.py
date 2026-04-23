"""Default Jinja2 templates for notification titles and bodies.

Single source of truth for the built-in, per-event-type defaults. The dispatcher
renders these directly; user-supplied `title_template` / `body_template` values
from ContentOptions override them. Keeping defaults here (not as Python code on
WatchEvent) means the UI can display and pre-fill them exactly as they'll render.

The change_detected default body contains only the always-present skeleton —
header (watch_name, URL, TIMESTAMP, WATCH dashboard link) and body block
(event_label, change_summary). Optional toggle-driven sections (DOMAIN, CHANGE,
diff, INTERVAL, LAST CHANGED, SIGNIFICANCE, DESCRIPTION, TAGS) are interleaved
in `src.core.notifications.content.build_body` at the issue-#104 positions.

`compose_body_prefill()` returns this skeleton verbatim; the "Show default
template" UX exposes it as the seed for users editing a custom body_template.

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
    TemplateVariable("watch_id", "str", "ULID of the watch", "always"),
    TemplateVariable("watch_name", "str", "Display name of the watch", "always"),
    TemplateVariable("watch_url", "str", "Monitored URL", "always"),
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
        "Pre-rendered diff lines (capped at ~10 entries)",
        "change_detected",
    ),
    TemplateVariable(
        "diff_full",
        "str",
        "Pre-rendered diff lines (all entries)",
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
    TemplateVariable("effective_domain", "str", "Resolved domain of the watch URL", "contextual"),
    TemplateVariable("check_interval", "str", 'Check cadence (e.g. "1h")', "contextual"),
    TemplateVariable("last_changed_at", "str", "Date of last detected change", "contextual"),
    TemplateVariable("tags", "list[str]", "Watch tags", "contextual"),
    TemplateVariable("description", "str", "Watch description", "contextual"),
]
"""Authoritative list of variables usable in user title/body templates.

Consumed by `partials/notification_variable_chips.html` (chip row) and the
expandable [See all variables] reference drawer. Keep in sync with
`src.core.notifications.content.build_template_context`.
"""

_TITLE = "[Observo] {{ event_label }}: {{ watch_name }}"


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


# Always-present skeleton for change_detected. Toggle-driven sections (DOMAIN,
# CHANGE, diff, INTERVAL, LAST CHANGED, SIGNIFICANCE, DESCRIPTION, TAGS) are
# interleaved by `build_body` in Python at the positions specified by the
# issue #104 layout. The WATCH: dashboard link is part of the unconditional
# default — there is no `include_watch_url` toggle.
_CHANGE_DETECTED_BODY = (
    "{{ watch_name }}\n"
    "URL: {{ watch_url }}\n"
    "TIMESTAMP: {{ occurred_at_iso }}\n"
    f"WATCH: {APP_URL}"
    "/watches/{{ watch_id }}\n"
    "\n"
    "{{ event_label }}\n"
    "{{ change_summary }}"
)


DEFAULT_BODY_TEMPLATES: dict[str, str] = {
    WatchEventType.CHANGE_DETECTED.value: _CHANGE_DETECTED_BODY,
    WatchEventType.WATCH_ERROR.value: (
        "{{ watch_url }} returned HTTP {{ status_code | default('unknown') }}"
    ),
    WatchEventType.WATCH_RECOVERED.value: "{{ watch_url }} is responding normally again",
    WatchEventType.WATCH_CREATED.value: "Now monitoring {{ watch_url }}",
    WatchEventType.WATCH_PAUSED.value: "Watch paused: {{ watch_url }}",
    WatchEventType.WATCH_RESUMED.value: "Watch resumed: {{ watch_url }}",
    WatchEventType.WATCH_ARCHIVED.value: "Watch archived: {{ watch_url }}",
    WatchEventType.WATCH_DELETED.value: "Watch deleted: {{ watch_url }}",
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
