"""WatchEventType enum and WatchEvent dataclass — universal notification envelope."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WatchEventType(enum.StrEnum):
    """Notification event type codes — the authoritative source for event types.

    Declaration order is roughly temporal (lifecycle order); drives UI presentation
    via EVENT_TITLES iteration. StrEnum values are stable and persisted as strings
    in the DB (``audit_log.event_type``, ``notification_templates.events``).
    """

    WATCH_CREATED = "watch_created"
    CHANGE_DETECTED = "change_detected"
    WATCH_ERROR = "watch_error"
    WATCH_RECOVERED = "watch_recovered"
    WATCH_PAUSED = "watch_paused"
    WATCH_RESUMED = "watch_resumed"
    WATCH_ARCHIVED = "watch_archived"
    WATCH_DELETED = "watch_deleted"


EVENT_TITLES: dict[str, str] = {
    WatchEventType.WATCH_CREATED.value: "Created",
    WatchEventType.CHANGE_DETECTED.value: "Change",
    WatchEventType.WATCH_ERROR.value: "Error",
    WatchEventType.WATCH_RECOVERED.value: "Recovered",
    WatchEventType.WATCH_PAUSED.value: "Paused",
    WatchEventType.WATCH_RESUMED.value: "Resumed",
    WatchEventType.WATCH_ARCHIVED.value: "Archived",
    WatchEventType.WATCH_DELETED.value: "Deleted",
}
"""Public mapping of event type value strings to human-readable titles.
Iteration order is roughly temporal (watch lifecycle); drives the Subscribe
checkbox order in the notification form. Used as a Jinja global in the dashboard
and as the `event_label` template context field."""


@dataclass(frozen=True)
class WatchEvent:
    """Immutable value object describing a watch lifecycle event.

    Titles and bodies are rendered by the dispatcher from Jinja templates
    (see `default_templates.py`); they are not properties on this class.
    """

    event_type: WatchEventType
    watched_item_id: str
    item_name: str
    item_url: str
    occurred_at: datetime
    metadata: dict = field(default_factory=dict)
