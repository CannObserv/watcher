"""Helpers for the pending_archiver_sync outbox."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.pending_archiver_sync import PendingArchiverSync

_BACKOFF_CAP_SECONDS = 3600


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff with a 1-hour cap. attempts=1 → 60s."""
    return min(60 * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)


async def select_due(session: AsyncSession, *, limit: int = 100) -> list[PendingArchiverSync]:
    """Return rows due for retry, oldest-first, with FOR UPDATE SKIP LOCKED."""
    result = await session.execute(
        select(PendingArchiverSync)
        .where(PendingArchiverSync.next_attempt_at <= datetime.now(UTC))
        .where(PendingArchiverSync.attempts < 10)
        .order_by(PendingArchiverSync.next_attempt_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


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
