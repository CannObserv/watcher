"""Shared utility functions for the watcher core."""

from datetime import UTC, datetime

from src.core.scheduling.resolution import resolved_schedule_config


def format_utc_iso(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with a ``Z`` suffix (AGENTS.md format).

    Coerces to UTC — naive datetimes are treated as UTC, non-UTC aware datetimes
    are converted — so the output always carries ``Z`` and accurately reflects UTC.
    Microseconds are preserved when present.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def watched_item_event_base_metadata(wi) -> dict:
    """Common WatchEvent metadata for a WatchedItem (#191).

    Used by the change-detected dispatch (pipeline) and error/recovery
    dispatches (tasks). Per-event keys are layered by the caller on top.

    ``check_interval`` is the cadence the scheduler runs — announced, else the
    system default (#375). It used to report the item default, which no longer
    schedules anything.
    """
    meta: dict = {}
    if wi.domain_name:
        meta["domain_name"] = wi.domain_name
    interval = resolved_schedule_config(wi).get("interval")
    if interval:
        meta["check_interval"] = interval
    if wi.last_changed_at:
        meta["last_changed_at"] = format_utc_iso(wi.last_changed_at)
    if wi.default_tags:
        meta["tags"] = wi.default_tags
    if wi.description:
        meta["description"] = wi.description
    return meta
