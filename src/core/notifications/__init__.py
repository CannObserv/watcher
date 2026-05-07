"""Notification subsystem — dispatch via the notifier service for watch lifecycle events."""

from src.core.notifications.events import WatchEvent, WatchEventType

__all__ = [
    "WatchEvent",
    "WatchEventType",
]
