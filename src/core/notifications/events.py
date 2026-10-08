"""WatchEventType enum and WatchEvent dataclass — universal notification envelope."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WatchEventType(enum.StrEnum):
    """Notification event type codes — the authoritative source for event types.

    Every member is subscribable, so every member must have a dispatch site:
    ``tests/test_notification_dispatch_sites.py`` fails on one that does not.
    #166 removed five that never fired (created, paused, resumed, archived,
    deleted) and stripped them from saved templates; their audits are the
    separate ``EventType``. Declaration order drives UI presentation via
    EVENT_TITLES iteration. StrEnum values are stable and persisted as strings
    in the DB (``notification_templates.events``, notification audit payloads).
    """

    CHANGE_DETECTED = "change_detected"
    WATCH_ERROR = "watch_error"
    WATCH_RECOVERED = "watch_recovered"


EVENT_TITLES: dict[str, str] = {
    WatchEventType.CHANGE_DETECTED.value: "Change",
    WatchEventType.WATCH_ERROR.value: "Error",
    WatchEventType.WATCH_RECOVERED.value: "Recovered",
}
"""Public mapping of event type value strings to human-readable titles.
Iteration order drives the Subscribe checkbox order in the notification form.
Used as a Jinja global in the dashboard and as the `event_label` template
context field."""


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
