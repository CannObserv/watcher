"""Outbox helpers for the `changes` table.

A Change is "unpublished" while `published_to_bus_at IS NULL`. The drain
worker selects unpublished rows, hands them to the ChangePublisher, then
marks them published.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.change import Change


async def select_unpublished(session: AsyncSession, *, limit: int = 100) -> list[Change]:
    """Return the oldest unpublished Changes, capped at `limit`."""
    result = await session.execute(
        select(Change)
        .where(Change.published_to_bus_at.is_(None))
        .order_by(Change.detected_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_published(session: AsyncSession, change_id: ULID, *, bus_message_id: str) -> None:
    """Mark a Change as published with the broker's message ID."""
    change = await session.get(Change, change_id)
    if change is None:
        return
    change.published_to_bus_at = datetime.now(UTC)
    change.bus_message_id = bus_message_id
