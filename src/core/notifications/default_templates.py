"""Default Jinja2 templates for notification titles and bodies.

Single source of truth for the built-in, per-event-type defaults. The dispatcher
renders these directly; user-supplied `title_template` / `body_template` values
from ContentOptions override them. Keeping defaults here (not as Python code on
WatchEvent) means the UI can display and pre-fill them exactly as they'll render.

Template context (shared with user templates) is built by
`src.core.notifications.content.build_template_context`.
"""

from src.core.notifications.events import WatchEventType

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
