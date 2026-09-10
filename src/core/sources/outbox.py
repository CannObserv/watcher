"""Helpers for the pending_archiver_sync outbox."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.pending_archiver_sync import PendingArchiverSync

_BACKOFF_CAP_SECONDS = 3600


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff with a 1-hour cap. attempts=1 → 60s."""
    return min(60 * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)


async def select_due(session: AsyncSession, *, limit: int = 100) -> list[PendingArchiverSync]:
    """Return rows due for retry, oldest-first, with FOR UPDATE SKIP LOCKED.

    Ordered by ``next_attempt_at``, ties broken by id. The tiebreak is not
    cosmetic since #291: a bulk backoff clear stamps one timestamp across the
    whole outbox, so the schedule alone stops being a total order, and ULIDs
    are time-ordered — the id restores insertion order rather than leaving it
    to the planner.

    Excludes dead-lettered rows only. The old ``attempts < 10`` ceiling is gone
    (#253): it silently stopped selecting a row without marking it, so an outage
    lasting ten backoffs abandoned revisions with no operator signal and nothing
    to find them by. Giving up is now explicit — see :func:`dead_letter` — and a
    transient broker failure never triggers it, because the outage is not the
    row's fault and there is no data-loss cliff worth having.
    """
    result = await session.execute(
        select(PendingArchiverSync)
        .where(PendingArchiverSync.next_attempt_at <= datetime.now(UTC))
        .where(PendingArchiverSync.dead_lettered_at.is_(None))
        .order_by(PendingArchiverSync.next_attempt_at.asc(), PendingArchiverSync.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


async def clear_backoffs(session: AsyncSession, *, exclude_ids: Sequence[ULID] = ()) -> int:
    """Pull failure-delayed rows forward to now; return how many moved (#291).

    A publish that succeeds is evidence about the **broker**, not about the row
    that carried it: every sibling still serving an exponential backoff is
    waiting on a condition that has demonstrably cleared. Without this, an
    operator who widens an ACL rule or frees the broker's memory waits out the
    3600 s cap before ``content.revisions`` resumes, and the recovery reads as
    stuck — which invites a second intervention against a system that is
    already fixed. Watcher is the only participant that schedules per row, so
    it is the only one with the lag: archiver retries on the next drain tick,
    replicator on its PEL reclaim.

    ``attempts > 0`` is the whole claim to be careful about. This clears
    *backoff*, not scheduling: a row delayed by something other than a failure
    is nothing this function knows how to reason about, so it is left alone.

    ``exclude_ids`` is for the rows the caller already decided this pass. A row
    that failed beside a success has been told something about itself rather
    than about the broker, and pulling it forward would retry it every tick —
    removing exactly the damping the backoff exists to provide.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(PendingArchiverSync)
        .where(PendingArchiverSync.dead_lettered_at.is_(None))
        .where(PendingArchiverSync.attempts > 0)
        .where(PendingArchiverSync.next_attempt_at > now)
        .where(PendingArchiverSync.id.notin_(list(exclude_ids)))
        .values(next_attempt_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


async def dead_letter(
    session: AsyncSession,
    row: PendingArchiverSync,
    *,
    error: str,
    reason: str,
) -> None:
    """Move ``row`` to its terminal state — it can never publish (#253).

    Records ``last_error`` and stamps ``dead_lettered_at``; the row is left in
    place for post-mortem, since the reason it is unpublishable is usually a
    field the row itself is missing. Does not touch ``attempts`` — the caller
    owns that counter.
    """
    row.last_error = f"{reason}: {error}"[:1000]
    row.dead_lettered_at = datetime.now(UTC)


async def mark_failure(
    session: AsyncSession,
    row: PendingArchiverSync,
    *,
    error: str,
) -> None:
    """Increment attempts, record error, advance next_attempt_at."""
    row.attempts += 1
    row.last_error = error
    row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=_backoff_seconds(row.attempts))


async def delete_pending(session: AsyncSession, row_id: ULID) -> None:
    """Remove a successfully-drained row."""
    row = await session.get(PendingArchiverSync, row_id)
    if row is not None:
        await session.delete(row)
