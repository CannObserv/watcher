"""Shared Pydantic validators reused across notification schemas."""

from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


def validate_event_list(events: list[str]) -> list[str]:
    """Raise ValueError if events is empty or contains unknown WatchEventType values."""
    if not events:
        raise ValueError("At least one event must be selected.")
    invalid = [e for e in events if e not in _VALID_EVENT_TYPES]
    if invalid:
        raise ValueError(
            f"Unknown event type(s): {invalid}. Valid types: {sorted(_VALID_EVENT_TYPES)}"
        )
    return events
