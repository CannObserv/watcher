"""WatchedItem CRUD API endpoints (#161)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import parse_ulid
from src.api.schemas.watched_item import WatchedItemPatch, WatchedItemResponse
from src.core.models.audit_log import EventType, audit
from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem

router = APIRouter(prefix="/watched-items", tags=["watched-items"])


async def _get_or_404(session: AsyncSession, wi_id: str) -> WatchedItem:
    """Fetch a WatchedItem by ID string, raising 404 if not found."""
    wi_ulid = parse_ulid(wi_id)
    wi = await session.get(WatchedItem, wi_ulid)
    if wi is None:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    return wi


@router.get("", response_model=list[WatchedItemResponse])
async def list_watched_items(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_db_session),
):
    """List WatchedItems. Archived excluded unless ``include_archived=true``."""
    stmt = select(WatchedItem).order_by(WatchedItem.name)
    if not include_archived:
        stmt = stmt.where(WatchedItem.archived_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{watched_item_id}", response_model=WatchedItemResponse)
async def get_watched_item(watched_item_id: str, session: AsyncSession = Depends(get_db_session)):
    """Fetch a single WatchedItem by ID."""
    return await _get_or_404(session, watched_item_id)


@router.patch("/{watched_item_id}", response_model=WatchedItemResponse)
async def patch_watched_item(
    watched_item_id: str,
    data: WatchedItemPatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Update mutable WatchedItem fields. All fields optional."""
    wi = await _get_or_404(session, watched_item_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(wi, field, value)
    if updates:
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=sorted(updates.keys()),
            source="api",
        )
    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/archive", response_model=WatchedItemResponse)
async def archive_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Archive a WatchedItem and cascade-archive all child Watches.

    The cascade flips ``is_active`` to False and ``is_archived`` to True on
    every child Watch in a single transaction; the WatchedItem's fetch
    cycle stops within one ``schedule_tick`` interval because the tick
    filters on ``WatchedItem.archived_at IS NULL``.
    """
    wi = await _get_or_404(session, watched_item_id)
    now = datetime.now(UTC)

    if wi.archived_at is None:
        wi.archived_at = now
        wi.is_active = False
        audit(
            session,
            EventType.WATCHED_ITEM_ARCHIVED,
            watched_item_id=str(wi.id),
            source="api",
        )
        result = await session.execute(select(Watch).where(Watch.watched_item_id == wi.id))
        for child in result.scalars().all():
            if not child.is_archived:
                child.is_active = False
                child.is_archived = True
                audit(
                    session,
                    EventType.WATCH_ARCHIVED,
                    watch_id=child.id,
                    cascade_from_watched_item_id=str(wi.id),
                    source="api",
                )

    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/restore", response_model=WatchedItemResponse)
async def restore_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Restore the WatchedItem only. Child Watches stay archived."""
    wi = await _get_or_404(session, watched_item_id)
    if wi.archived_at is not None:
        wi.archived_at = None
        wi.is_active = True
        audit(
            session,
            EventType.WATCHED_ITEM_RESTORED,
            watched_item_id=str(wi.id),
            source="api",
        )
    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/mark-reviewed", response_model=WatchedItemResponse)
async def mark_reviewed(watched_item_id: str, session: AsyncSession = Depends(get_db_session)):
    """Stamp ``last_reviewed_at = now()``."""
    wi = await _get_or_404(session, watched_item_id)
    wi.last_reviewed_at = datetime.now(UTC)
    audit(
        session,
        EventType.WATCHED_ITEM_REVIEWED,
        watched_item_id=str(wi.id),
        source="api",
    )
    await session.commit()
    await session.refresh(wi)
    return wi
