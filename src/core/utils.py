"""Shared utility functions for the watcher core."""

from datetime import UTC, datetime


def format_utc_iso(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with a ``Z`` suffix (AGENTS.md format).

    Coerces to UTC — naive datetimes are treated as UTC, non-UTC aware datetimes
    are converted — so the output always carries ``Z`` and accurately reflects UTC.
    Microseconds are preserved when present.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
