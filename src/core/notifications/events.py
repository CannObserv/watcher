"""WatchEventType enum and WatchEvent dataclass — universal notification envelope."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class WatchEventType(enum.StrEnum):
    """Notification event type codes. Values mirror the notification_event_types DB table."""

    CHANGE_DETECTED = "change_detected"
    WATCH_ERROR = "watch_error"
    WATCH_RECOVERED = "watch_recovered"
    WATCH_CREATED = "watch_created"
    WATCH_PAUSED = "watch_paused"
    WATCH_RESUMED = "watch_resumed"
    WATCH_ARCHIVED = "watch_archived"
    WATCH_DELETED = "watch_deleted"


_TITLES: dict[WatchEventType, str] = {
    WatchEventType.CHANGE_DETECTED: "Change Detected",
    WatchEventType.WATCH_ERROR: "Watch Error",
    WatchEventType.WATCH_RECOVERED: "Watch Recovered",
    WatchEventType.WATCH_CREATED: "Watch Created",
    WatchEventType.WATCH_PAUSED: "Watch Paused",
    WatchEventType.WATCH_RESUMED: "Watch Resumed",
    WatchEventType.WATCH_ARCHIVED: "Watch Archived",
    WatchEventType.WATCH_DELETED: "Watch Deleted",
}

EVENT_TITLES: dict[str, str] = {e.value: t for e, t in _TITLES.items()}
"""Public mapping of event type value strings to human-readable titles."""

_BODY_TEMPLATES: dict[WatchEventType, str] = {
    WatchEventType.WATCH_RECOVERED: "{watch_url} is responding normally again",
    WatchEventType.WATCH_CREATED: "Now monitoring {watch_url}",
    WatchEventType.WATCH_PAUSED: "Watch paused: {watch_url}",
    WatchEventType.WATCH_RESUMED: "Watch resumed: {watch_url}",
    WatchEventType.WATCH_ARCHIVED: "Watch archived: {watch_url}",
    WatchEventType.WATCH_DELETED: "Watch deleted: {watch_url}",
}

_APPRISE_TYPES: dict[WatchEventType, str] = {
    WatchEventType.CHANGE_DETECTED: "info",
    WatchEventType.WATCH_ERROR: "failure",
    WatchEventType.WATCH_RECOVERED: "success",
    WatchEventType.WATCH_CREATED: "info",
    WatchEventType.WATCH_PAUSED: "warning",
    WatchEventType.WATCH_RESUMED: "info",
    WatchEventType.WATCH_ARCHIVED: "warning",
    WatchEventType.WATCH_DELETED: "warning",
}


@dataclass(frozen=True)
class WatchEvent:
    """Immutable value object describing a watch lifecycle event."""

    event_type: WatchEventType
    watch_id: str
    watch_name: str
    watch_url: str
    occurred_at: datetime
    metadata: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        """Short notification title including watch name."""
        return f"{_TITLES[self.event_type]}: {self.watch_name}"

    @property
    def body(self) -> str:
        """Human-readable notification body."""
        if self.event_type == WatchEventType.CHANGE_DETECTED:
            parts: list[str] = []
            for label in ("added", "modified", "removed"):
                items = self.metadata.get(label, [])
                if items:
                    parts.append(f"{len(items)} {label}")
            detail = ", ".join(parts) if parts else "details pending"
            return f"{self.watch_url} — {detail}"
        if self.event_type == WatchEventType.WATCH_ERROR:
            status = self.metadata.get("status_code", "unknown")
            return f"{self.watch_url} returned HTTP {status}"
        template = _BODY_TEMPLATES.get(self.event_type)
        if template:
            return template.format(watch_url=self.watch_url)
        return self.watch_url

    @property
    def apprise_notify_type(self) -> str:
        """Apprise NotifyType string for this event type."""
        return _APPRISE_TYPES[self.event_type]
