"""Canned mock event data for the notification preview endpoint.

A stateless preview needs to render a realistic WatchEvent without touching the
database. `MOCK_EVENT_FIXTURES` holds one fixture per event type, keyed by the
string `WatchEventType` value. `build_preview_event()` wraps the fixture in a
`WatchEvent` suitable for passing through `build_title` / `build_body`.

**Fidelity invariant (#221).** Each fixture's metadata keys must be a subset of
the keys the real emitters actually produce for that event — otherwise the
preview shows fields a delivered notification never carries. The shared base
mirrors `watched_item_event_base_metadata`; the per-event extras mirror what
`pipeline.py` / `tasks.py` layer on. `tests/core/notifications/test_preview_fixtures.py`
guards this against drift.

The diff pipeline (Snapshot → Change → unified diff) was removed in Phase 5
(#156); the diff/significance fixture fields and `compute_preview_unified_diff`
were removed in #221 along with the toggles that consumed them (restoration
tracked in #222).
"""

from datetime import UTC, datetime

from src.core.notifications.events import WatchEvent, WatchEventType

_PREVIEW_WATCH_ID = "01KPPFATBNYQGBB38SQ06DN9HY"
_PREVIEW_WATCH_NAME = "Example Watch"
_PREVIEW_WATCH_URL = "https://example.com/regulatory-page"
_PREVIEW_OCCURRED_AT = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


# Mirrors `watched_item_event_base_metadata` (src/core/utils.py): the context
# every dispatch layers onto the event before adding per-event keys.
_SHARED_CONTEXT = {
    "domain_name": "example.com",
    "check_interval": "1h",
    "last_changed_at": "2026-04-15T03:22:00Z",
    "tags": ["regulatory", "filings"],
    "description": "Tracks regulatory filings page",
}


MOCK_EVENT_FIXTURES: dict[str, dict] = {
    WatchEventType.CHANGE_DETECTED.value: {
        **_SHARED_CONTEXT,
        # Layered by pipeline.py on change detection.
        "change_revision_id": "01KPPFATBNYQGBB38SQ06DN9HZ",
        "content_fingerprint": "sha256:9f2c1e",
    },
    WatchEventType.WATCH_ERROR.value: {
        **_SHARED_CONTEXT,
        "status_code": 503,
    },
    WatchEventType.WATCH_RECOVERED.value: {**_SHARED_CONTEXT},
    WatchEventType.WATCH_CREATED.value: {**_SHARED_CONTEXT},
    WatchEventType.WATCH_PAUSED.value: {**_SHARED_CONTEXT},
    WatchEventType.WATCH_RESUMED.value: {**_SHARED_CONTEXT},
    WatchEventType.WATCH_ARCHIVED.value: {**_SHARED_CONTEXT},
    WatchEventType.WATCH_DELETED.value: {**_SHARED_CONTEXT},
}


def build_preview_event(event_type: str) -> WatchEvent:
    """Build a WatchEvent instance seeded with fixture metadata for `event_type`.

    Raises KeyError for unknown event_type values.
    """
    metadata = MOCK_EVENT_FIXTURES[event_type]
    return WatchEvent(
        event_type=WatchEventType(event_type),
        watched_item_id=_PREVIEW_WATCH_ID,
        item_name=_PREVIEW_WATCH_NAME,
        item_url=_PREVIEW_WATCH_URL,
        occurred_at=_PREVIEW_OCCURRED_AT,
        metadata=metadata,
    )
