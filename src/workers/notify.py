"""Re-export dispatch_event_notifications from src.core.notifications.notify.

This module is a backwards-compatibility shim. New code should import from
src.core.notifications.notify directly.
"""

from src.core.notifications.notify import DispatchCandidate, dispatch_event_notifications

__all__ = ["DispatchCandidate", "dispatch_event_notifications"]
