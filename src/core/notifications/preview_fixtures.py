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

**Diff parity (#222).** The diff is never event metadata — the dispatcher
computes it from the two stored canonical texts the event's fingerprints name.
The preview stands in two canned texts, names them by their real digests, and
diffs them with the dispatcher's own ``compute_unified_diff`` (``preview_diff``),
so the preview shows exactly what a delivered notification would.
`test_preview_fixtures.TestPreviewDispatchParity` drives the dispatcher over
this fixture and compares bodies.
"""

from datetime import UTC, datetime

from co_core.pure.util.hashing import prefixed_sha256, sha256

from src.core.notifications.diff import ChangeDiff, compute_unified_diff
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


# Canonical extracted text — chunk texts joined by "\n" (cannobserv#486), the
# shape the processor stores and the dispatcher diffs. Not HTML: the diff is
# over what the fingerprint hashes, never the raw page.
PREVIEW_PREVIOUS_TEXT = b"""\
Regulatory Filings
Last updated: 2026-04-10
Hours
Mon-Fri: 9:00 - 17:00
Contact
contact@example.com
Recent filings
Application 2026-04-08
Renewal 2026-04-09"""

PREVIEW_CURRENT_TEXT = b"""\
Regulatory Filings
Last updated: 2026-04-15
New licensing program
Apply for a license at https://example.com/apply
Contact
support@example.com
Recent filings
Application 2026-04-08
Renewal 2026-04-12
Renewal 2026-04-15"""


MOCK_EVENT_FIXTURES: dict[str, dict] = {
    WatchEventType.CHANGE_DETECTED.value: {
        **_SHARED_CONTEXT,
        # Layered by pipeline.py on change detection.
        "change_revision_id": "01KPPFATBNYQGBB38SQ06DN9HZ",
        "previous_fingerprint": prefixed_sha256(sha256(PREVIEW_PREVIOUS_TEXT)),
        "current_fingerprint": prefixed_sha256(sha256(PREVIEW_CURRENT_TEXT)),
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


def preview_diff(event_type: str) -> ChangeDiff | None:
    """The diff the dispatcher would compute for the preview event, or ``None``.

    Only a change carries one; computed inline, since the canned texts are tiny.
    """
    if event_type != WatchEventType.CHANGE_DETECTED.value:
        return None
    return ChangeDiff(unified=compute_unified_diff(PREVIEW_PREVIOUS_TEXT, PREVIEW_CURRENT_TEXT))
