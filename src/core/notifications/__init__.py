"""Notification subsystem — Apprise-based dispatch for watch lifecycle events."""

from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType

__all__ = [
    "WatchEvent",
    "WatchEventType",
    "dispatch_event",
]
