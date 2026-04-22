"""Default Jinja2 templates for notification titles and bodies.

Single source of truth for the built-in, per-event-type defaults. The dispatcher
renders these directly; user-supplied `title_template` / `body_template` values
from ContentOptions override them. Keeping defaults here (not as Python code on
WatchEvent) means the UI can display and pre-fill them exactly as they'll render.

`ADDITIVE_BODY_SNIPPETS` + `compose_body_prefill()` power the "toggles seed the
pre-fill" flow: when a user clicks [Edit template] on the body block, the
textarea is pre-populated with the default body template plus a Jinja snippet
for every currently-enabled additive toggle — giving the user a complete,
runnable starting template rather than a blank textarea.

Template context (shared with user templates) is built by
`src.core.notifications.content.build_template_context`.
"""

from dataclasses import dataclass

from src.api.schemas.content_config import ContentOptions
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

_TITLE = "{{ event_label }}: {{ watch_name }}"


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


DEFAULT_BODY_TEMPLATES: dict[str, str] = {
    WatchEventType.CHANGE_DETECTED.value: "{{ watch_url }} — {{ change_summary }}",
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


# Jinja snippets corresponding to ContentOptions `include_<name>` toggles.
# Each snippet guards on the relevant metadata key so rendering is safe even
# when the field is absent. Keys match the toggle name (sans `include_`).
_DIFF_SNIPPET_JINJA = (
    "{%- if added or removed or modified %}"
    "\nChanged sections:"
    "{%- for label in added %}\n  + {{ label }}{%- endfor %}"
    "{%- for label in removed %}\n  - {{ label }}{%- endfor %}"
    "{%- for item in modified %}\n  ~ {{ item.label }} "
    "({{ (item.similarity * 100) | int }}% similar){%- endfor %}"
    "{%- endif %}"
)


ADDITIVE_BODY_SNIPPETS: dict[str, str] = {
    "diff_snippet": _DIFF_SNIPPET_JINJA,
    "diff_full": _DIFF_SNIPPET_JINJA,
    "temporal_context": (
        "{%- if check_interval %}Check interval: {{ check_interval }}{%- endif %}"
    ),
    "domain": "{%- if effective_domain %}Domain: {{ effective_domain }}{%- endif %}",
    "last_changed_at": ("{%- if last_changed_at %}Last changed: {{ last_changed_at }}{%- endif %}"),
    "significance": (
        "{%- if significance is not none %}"
        "Significance: {{ (significance * 100) | int }}%"
        "{%- endif %}"
    ),
    "change_dashboard_url": "{%- if change_url %}View change: {{ change_url }}{%- endif %}",
    "watch_url": f"Watch URL: {APP_URL}" + "/watches/{{ watch_id }}",
    "tags": "{%- if tags %}Tags: {{ tags | join(', ') }}{%- endif %}",
    "description": ("{%- if description %}Description: {{ description }}{%- endif %}"),
}


def compose_title_prefill(event_type: str) -> str:
    """Return the default title Jinja template for this event type.

    Used to pre-fill the title textarea when a user first clicks [Edit template].
    There are no additive toggles for titles, so this is just the default.
    """
    return DEFAULT_TITLE_TEMPLATES[event_type]


def compose_body_prefill(event_type: str, options: ContentOptions) -> str:
    """Return the default body Jinja plus enabled additive snippets, joined.

    Each currently-enabled `include_<name>` toggle contributes its Jinja snippet
    from `ADDITIVE_BODY_SNIPPETS`; sections are joined with blank lines, matching
    the format `build_body` produces at dispatch time. The output is a runnable
    Jinja template the user can edit.
    """
    parts: list[str] = [DEFAULT_BODY_TEMPLATES[event_type]]
    # Iterate in a stable order matching the form layout. The Links section
    # in notification_form_content_body.html lists Watch URL first, Change
    # URL second — keep prefill output in the same order.
    for name in (
        "diff_snippet",
        "diff_full",
        "temporal_context",
        "domain",
        "last_changed_at",
        "significance",
        "watch_url",
        "change_dashboard_url",
        "tags",
        "description",
    ):
        if getattr(options, f"include_{name}"):
            parts.append(ADDITIVE_BODY_SNIPPETS[name])
    return "\n\n".join(parts)
