"""WatchEventType enum and WatchEvent dataclass — universal notification envelope."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WatchEventType(enum.StrEnum):
    """Notification event type codes. Values mirror the notification_event_types DB table.

    Declaration order is roughly temporal (lifecycle order); drives UI presentation
    via EVENT_TITLES iteration. StrEnum values are stable and persisted in the DB.
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
    WatchEventType.WATCH_CREATED.value: "Watch Created",
    WatchEventType.CHANGE_DETECTED.value: "Change Detected",
    WatchEventType.WATCH_ERROR.value: "Watch Error",
    WatchEventType.WATCH_RECOVERED.value: "Watch Recovered",
    WatchEventType.WATCH_PAUSED.value: "Watch Paused",
    WatchEventType.WATCH_RESUMED.value: "Watch Resumed",
    WatchEventType.WATCH_ARCHIVED.value: "Watch Archived",
    WatchEventType.WATCH_DELETED.value: "Watch Deleted",
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
    watch_id: str
    watch_name: str
    watch_url: str
    occurred_at: datetime
    metadata: dict = field(default_factory=dict)
