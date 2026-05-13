"""Helpers for the pending_source_revisions outbox."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.pending_source_revision import PendingSourceRevision

_BACKOFF_CAP_SECONDS = 3600


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff with a 1-hour cap. attempts=1 → 60s."""
    return min(60 * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)


async def enqueue_pending(
    session: AsyncSession,
    *,
    info_source_id: str,
    content_fingerprint: str,
    captured_at: datetime,
    content_cache_uri: str,
    content_cache_expires_at: datetime,
    content_size_bytes: int | None = None,
    content_media_type: str | None = None,
) -> PendingSourceRevision:
    """Insert a new outbox row. next_attempt_at = now."""
    row = PendingSourceRevision(
        info_source_id=info_source_id,
        content_fingerprint=content_fingerprint,
        captured_at=captured_at,
        content_size_bytes=content_size_bytes,
        content_media_type=content_media_type,
        content_cache_uri=content_cache_uri,
        content_cache_expires_at=content_cache_expires_at,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def select_due(session: AsyncSession, *, limit: int = 100) -> list[PendingSourceRevision]:
    """Return rows due for retry, oldest-first, with FOR UPDATE SKIP LOCKED."""
    result = await session.execute(
        select(PendingSourceRevision)
        .where(PendingSourceRevision.next_attempt_at <= datetime.now(UTC))
        .where(PendingSourceRevision.attempts < 10)
        .order_by(PendingSourceRevision.next_attempt_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


async def mark_failure(
    session: AsyncSession,
    row: PendingSourceRevision,
    *,
    error: str,
) -> None:
    """Increment attempts, record error, advance next_attempt_at."""
    row.attempts += 1
    row.last_error = error
    row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=_backoff_seconds(row.attempts))


async def delete_pending(session: AsyncSession, row_id: ULID) -> None:
    """Remove a successfully-drained row."""
    row = await session.get(PendingSourceRevision, row_id)
    if row is not None:
        await session.delete(row)
