"""Live-inheritance resolvers — Watch override → WatchedItem default → system default.

Per design Section 4 (#160). Resolution is performed at read time so edits to a
WatchedItem propagate immediately to all child Watches that do not override the
field. Tags merge additively (union); scalars override.

Notification dispatch (the Approach B union of WatchedItem templates +
per-Watch configs) is implemented inline in
``src/core/notifications/notify.py``; there is no separate resolver function
because the dispatcher already operates directly on the row objects.
"""

from src.core.models.watch import ContentType, Watch
from src.core.utils import format_utc_iso

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}
SYSTEM_DEFAULT_CONTENT_TYPE: ContentType = ContentType.HTML


def resolved_schedule_config(watch: Watch) -> dict:
    """Schedule lives on WatchedItem only — Watch has no per-row override.

    Distinguishes `None` (no override set) from `{}` (explicitly empty override).
    Empty dict passes through; `None` falls back to the system default.
    """
    wi = watch.watched_item
    if wi is not None and wi.default_schedule_config is not None:
        return wi.default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG


def resolved_content_type(watch: Watch) -> ContentType:
    """Resolve content_type: Watch override → WatchedItem default → system default."""
    if watch.content_type is not None:
        return watch.content_type
    wi = watch.watched_item
    if wi is not None and wi.default_content_type is not None:
        return wi.default_content_type
    return SYSTEM_DEFAULT_CONTENT_TYPE


def resolved_tags(watch: Watch) -> list[str]:
    """Additive merge: WatchedItem.default_tags ∪ Watch.tags, sorted."""
    wi_tags = (watch.watched_item.default_tags if watch.watched_item else None) or []
    own = watch.tags or []
    return sorted(set(wi_tags) | set(own))


def watch_event_base_metadata(watch: Watch) -> dict:
    """Common metadata fields shared across WatchEvent dispatches.

    Used by both the change-detected dispatch in ``src/workers/pipeline.py``
    and the error/recovery dispatches in ``src/workers/tasks.py``. Per-event
    keys (``source_revision_id``, ``status_code``, etc.) are layered by the
    caller on top of this base.

    Interval is read via the resolution chain so a WatchedItem-level edit
    propagates without a per-Watch update.
    """
    meta: dict = {}
    domain = watch.watched_item.domain_name if watch.watched_item else None
    if domain:
        meta["domain_name"] = domain
    interval = resolved_schedule_config(watch).get("interval")
    if interval:
        meta["check_interval"] = interval
    if watch.watched_item and watch.watched_item.last_changed_at:
        meta["last_changed_at"] = format_utc_iso(watch.watched_item.last_changed_at)
    if watch.tags:
        meta["tags"] = watch.tags
    if watch.description:
        meta["description"] = watch.description
    return meta
