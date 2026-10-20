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
post-mortem. ``WATCH_EVENT_EMITS`` is that validation's lookup.

**``check_rejected`` is the one fact that is not an edge** (#371): the outcome
of a ``watch_check`` command Watcher will not run, staged in the consumer's
commit. It takes the ids rather than an item, since ``unknown_item`` has none.
"""

from datetime import UTC, datetime

from co_core.pure.models.changes import (
    ChangeDetectedEmit,
    CheckRejectedEmit,
    WatchErrorEmit,
    WatchRecoveredEmit,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.base import generate_ulid
from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.models.watched_item import WatchedItem
from src.core.utils import format_utc_iso

#: ``PendingWatchEvent.event_type`` → the Emit twin the drain validates with.
WATCH_EVENT_EMITS: dict[str, type[BaseModel]] = {
    "change_detected": ChangeDetectedEmit,
    "check_rejected": CheckRejectedEmit,
    "watch_error": WatchErrorEmit,
    "watch_recovered": WatchRecoveredEmit,
}


# The last event id this process minted (CR 2). The drain publishes in id
# order, so ids must sort in staging order — and a check that recovers *and*
# changes stages two facts in one commit, usually inside one millisecond,
# where a random ULID sorts either way. One process stages every fact
# (AGENTS.md → *Single process is load-bearing*), so a process-local floor is
# the whole guarantee.
_last_event_id: ULID | None = None

#: ``pending_watch_events.info_item_id``'s width: a ULID.
INFO_ITEM_ID_COLUMN_LEN = 26


def _mint_event_id() -> ULID:
    """A fresh ULID that sorts after every id this process minted before."""
    global _last_event_id
    event_id = generate_ulid()
    if _last_event_id is not None and event_id <= _last_event_id:
        event_id = ULID.from_int(int(_last_event_id) + 1)
    _last_event_id = event_id
    return event_id


def _iso(value: datetime | None) -> str | None:
    return None if value is None else format_utc_iso(value)


def _stage_row(
    session: AsyncSession,
    event_type: str,
    *,
    info_item_id: str,
    info_source_id: str | None,
    now: datetime,
    fields: dict,
) -> PendingWatchEvent:
    """Add one outbox row; the minted id is the fact's ``event_id``.

    The ``info_item_id`` column is a query aid sized for a ULID; the payload
    carries the id verbatim. Only a ``check_rejected`` for an id Watcher could
    never hold exceeds it (#371), and that row is clipped, not refused.
    """
    event_id = _mint_event_id()
    row = PendingWatchEvent(
        id=event_id,
        event_type=event_type,
        info_item_id=info_item_id[:INFO_ITEM_ID_COLUMN_LEN],
        payload={
            "occurred_at": format_utc_iso(now),
            "event_id": str(event_id),
            "info_item_id": info_item_id,
            "info_source_id": info_source_id,
            **fields,
        },
        next_attempt_at=datetime.now(UTC),
    )
    session.add(row)
    return row


def _stage(
    session: AsyncSession,
    watched_item: WatchedItem,
    event_type: str,
    *,
    now: datetime,
    fields: dict,
) -> PendingWatchEvent:
    """Stage an edge of ``watched_item``: its own ids name the fact."""
    return _stage_row(
        session,
        event_type,
        info_item_id=str(watched_item.archiver_info_item_id),
        info_source_id=str(watched_item.archiver_info_source_id),
        now=now,
        fields=fields,
    )


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


def enqueue_check_rejected(
    session: AsyncSession,
    *,
    info_item_id: str,
    info_source_id: str | None,
    command_id: str,
    reason: str,
    now: datetime,
) -> PendingWatchEvent:
    """Stage the rejection of ``watch_check`` command ``command_id`` (#371).

    ``info_item_id`` is the command's, verbatim; ``info_source_id`` is ``None``
    exactly when ``reason`` is ``unknown_item``, which the Emit checks at drain.
    """
    return _stage_row(
        session,
        "check_rejected",
        info_item_id=info_item_id,
        info_source_id=info_source_id,
        now=now,
        fields={"command_id": command_id, "reason": reason},
    )
