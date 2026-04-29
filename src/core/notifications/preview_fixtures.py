"""Canned mock event data for the notification preview endpoint.

A stateless preview needs to render a realistic WatchEvent without touching the
database. `MOCK_EVENT_FIXTURES` holds one fixture per event type, keyed by the
string `WatchEventType` value. `build_preview_event()` wraps the fixture in a
`WatchEvent` suitable for passing through `build_title` / `build_body`.

For change_detected, the fixture exposes ``previous_text`` / ``current_text``
as HTML strings. `compute_preview_unified_diff()` passes them through
`normalize_html` (html5lib + lxml pretty-print) before diffing, mirroring the
dispatcher's HTML-watch path so preview line counts and structure match
production output (#125).

Preview is fixture-only for v1. A future extension could source a fixture from
a real recent watch event for a given user — tracked in the design doc under
"Out of scope (v1)".
"""

from datetime import UTC, datetime

from src.core.diff.normalize import normalize_html
from src.core.diff.textual import compute_unified_diff
from src.core.notifications.events import WatchEvent, WatchEventType

_PREVIEW_WATCH_ID = "01KPPFATBNYQGBB38SQ06DN9HY"
_PREVIEW_WATCH_NAME = "Example Watch"
_PREVIEW_WATCH_URL = "https://example.com/regulatory-page"
_PREVIEW_OCCURRED_AT = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


_SHARED_CONTEXT = {
    "effective_domain": "example.com",
    "check_interval": "1h",
    "last_changed_at": "2026-04-15T03:22:00Z",
    "tags": ["regulatory", "filings"],
    "description": "Tracks regulatory filings page",
}


_PREVIEW_PREVIOUS_TEXT = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Cannabis Observer — Regulatory Filings</title></head>
<body>
<h1>Cannabis Observer — Regulatory Filings</h1>
<p>Last updated: 2026-04-10</p>
<section id="hours">
  <h2>Hours</h2>
  <p>Mon-Fri: 9:00 - 17:00</p>
</section>
<section id="contact">
  <h2>Contact</h2>
  <p><a href="mailto:contact@example.com">contact@example.com</a></p>
</section>
<section id="filings">
  <h2>Recent filings</h2>
  <ul>
    <li>Application 2026-04-08</li>
    <li>Renewal 2026-04-09</li>
  </ul>
</section>
<footer>Footer</footer>
</body>
</html>
"""

_PREVIEW_CURRENT_TEXT = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Cannabis Observer — Regulatory Filings</title></head>
<body>
<h1>Cannabis Observer — Regulatory Filings</h1>
<p>Last updated: 2026-04-15</p>
<section id="licensing">
  <h2>New licensing program</h2>
  <p>Apply for a license at <a href="https://example.com/apply">https://example.com/apply</a></p>
</section>
<section id="contact">
  <h2>Contact</h2>
  <p><a href="mailto:support@example.com">support@example.com</a></p>
</section>
<section id="filings">
  <h2>Recent filings</h2>
  <ul>
    <li>Application 2026-04-08</li>
    <li>Renewal 2026-04-12</li>
    <li>Renewal 2026-04-15</li>
  </ul>
</section>
<footer>Footer</footer>
</body>
</html>
"""


MOCK_EVENT_FIXTURES: dict[str, dict] = {
    WatchEventType.CHANGE_DETECTED.value: {
        **_SHARED_CONTEXT,
        "added": ["New licensing section"],
        "modified": [{"label": "Contact information", "similarity": 0.72}],
        "removed": ["Deprecated hours section"],
        "significance": 0.65,
        "change_id": "01KPPFATBNYQGBB38SQ06DN9HZ",
        "previous_text": _PREVIEW_PREVIOUS_TEXT,
        "current_text": _PREVIEW_CURRENT_TEXT,
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
        watch_id=_PREVIEW_WATCH_ID,
        watch_name=_PREVIEW_WATCH_NAME,
        watch_url=_PREVIEW_WATCH_URL,
        occurred_at=_PREVIEW_OCCURRED_AT,
        metadata=metadata,
    )


def compute_preview_unified_diff(event_type: str) -> str:
    """Return the unified diff for a preview event, or '' for non-diff events.

    Computed live from the fixture's `previous_text` / `current_text` so the
    preview path stays stateless and never touches storage. Empty string for
    event types that don't carry diff text (everything except change_detected).
    """
    metadata = MOCK_EVENT_FIXTURES.get(event_type, {})
    prev = metadata.get("previous_text")
    curr = metadata.get("current_text")
    if not prev or not curr:
        return ""
    try:
        prev, curr = normalize_html(prev), normalize_html(curr)
    except Exception:
        pass  # fixture HTML is well-formed; failure here is unexpected but non-fatal
    return compute_unified_diff(prev, curr).unified_diff
