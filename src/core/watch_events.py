"""The ``info.watch-events`` outbox writers (#369).

Watcher reports its edges to Archiver as facts (cannobserv
``docs/change_bus/watch.md``): ``change_detected`` when the content changed,
``watch_error`` on the edge **into** error from any other health, and
``watch_recovered`` on error → ok. Each writer stages one
``PendingWatchEvent`` in the caller's session and commits nothing — the row
must land in the same commit as the state change it reports, and the edge's
own commit is that one. ``drain_pending_watch_events`` publishes it.

**Nothing else is an edge.** A baseline, a re-baseline (Option A), a #293
renewal and a repeated failure inside an episode stage nothing: re-notify
cadence is Archiver's, timed from the error edge plus the
``info.watch-status`` ``health`` level.

**The payload is validated at drain time, never here.** A refusal at the edge
would raise inside the transaction that records the health change and roll it
back — the hazard co-core's ``WatchHealthReason`` comment names. The drain
dead-letters a payload its Emit refuses instead, and the row survives for
post-mortem. ``WATCH_EVENT_EMITS`` is that validation's lookup; #371's
``check_rejected`` joins it with no schema change.
"""

from datetime import UTC, datetime

from co_core.pure.models.changes import (
    ChangeDetectedEmit,
    WatchErrorEmit,
    WatchRecoveredEmit,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.base import generate_ulid
from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.models.watched_item import WatchedItem
from src.core.utils import format_utc_iso

#: ``PendingWatchEvent.event_type`` → the Emit twin the drain validates with.
WATCH_EVENT_EMITS: dict[str, type[BaseModel]] = {
    "change_detected": ChangeDetectedEmit,
    "watch_error": WatchErrorEmit,
    "watch_recovered": WatchRecoveredEmit,
}


def _iso(value: datetime | None) -> str | None:
    return None if value is None else format_utc_iso(value)


def _stage(
    session: AsyncSession,
    watched_item: WatchedItem,
    event_type: str,
    *,
    now: datetime,
    fields: dict,
) -> PendingWatchEvent:
    """Add one outbox row; the minted id is the fact's ``event_id``."""
    event_id = generate_ulid()
    info_item_id = str(watched_item.archiver_info_item_id)
    row = PendingWatchEvent(
        id=event_id,
        event_type=event_type,
        info_item_id=info_item_id,
        payload={
            "occurred_at": format_utc_iso(now),
            "event_id": str(event_id),
            "info_item_id": info_item_id,
            "info_source_id": str(watched_item.archiver_info_source_id),
            **fields,
        },
        next_attempt_at=datetime.now(UTC),
    )
    session.add(row)
    return row


def enqueue_watch_error(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    reason: str | None,
    status_code: int | None,
) -> PendingWatchEvent:
    """Stage the edge into error. ``reason`` is the ``health_reason`` written
    at the same edge; ``None`` is staged as-is and dead-letters at the drain."""
    return _stage(
        session,
        watched_item,
        "watch_error",
        now=now,
        fields={"reason": reason, "status_code": status_code},
    )


def enqueue_watch_recovered(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    error_since: datetime | None,
) -> PendingWatchEvent:
    """Stage error → ok. ``error_since`` is the episode's start, ``None`` for an
    episode opened before it was recorded — stated, never omitted."""
    return _stage(
        session,
        watched_item,
        "watch_recovered",
        now=now,
        fields={"error_since": _iso(error_since)},
    )


def enqueue_change_detected(
    session: AsyncSession,
    watched_item: WatchedItem,
    *,
    now: datetime,
    previous_fingerprint: str,
    current_fingerprint: str,
    previous_text_uri: str | None,
    current_text_uri: str | None,
    extraction_changed: str | None,
    previous_changed_at: datetime | None,
) -> PendingWatchEvent:
    """Stage a notified content change. The fingerprints are the two texts'
    digests, and each URI is where the processor stored that text;
    ``previous_text_uri`` is ``None`` for a text stored before shadow mode."""
    return _stage(
        session,
        watched_item,
        "change_detected",
        now=now,
        fields={
            "previous_fingerprint": previous_fingerprint,
            "current_fingerprint": current_fingerprint,
            "previous_text_uri": previous_text_uri,
            "current_text_uri": current_text_uri,
            "extraction_changed": extraction_changed,
            "previous_changed_at": _iso(previous_changed_at),
        },
    )
