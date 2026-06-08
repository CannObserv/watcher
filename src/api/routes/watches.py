"""Watch CRUD API endpoints (#185 Phase A step 7 — WatchedItem-first)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import get_watch_or_404
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.watches import create_watch as _create_watch

logger = get_logger(__name__)

router = APIRouter(prefix="/watches", tags=["watches"])


@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a Watch under an existing WatchedItem.

    The WatchedItem must already exist with its effective_url set. No Archiver
    SDK calls at Watch-create time (#185 Phase A).

    Error mapping:
    - ``ValueError`` (watched_item_id not found) → 422.
    """
    try:
        watch = await _create_watch(
            session=session,
            name=data.name,
            watched_item_id=data.watched_item_id,
            content_type=data.content_type,
            description=data.description,
            tags=data.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return watch


@router.get("", response_model=list[WatchResponse])
async def list_watches(
    is_active: bool | None = None,
    is_archived: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """List all watches, optionally filtered by active or archived status.

    Omitting ``is_archived`` returns all watches regardless of archive status.
    Pass ``is_archived=false`` to exclude archived watches, or ``is_archived=true``
    to return only archived watches.
    """
    stmt = select(Watch).order_by(Watch.created_at.desc())
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    if is_archived is not None:
        stmt = stmt.where(Watch.is_archived.is_(is_archived))
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/{watch_id}", response_model=WatchResponse)
async def get_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Get a watch by ID."""
    return await get_watch_or_404(watch_id, session)


@router.patch("/{watch_id}", response_model=WatchResponse)
async def update_watch(
    watch_id: str,
    data: WatchUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update a watch. Only provided fields are changed."""
    watch = await get_watch_or_404(watch_id, session)

    updates = data.model_dump(exclude_unset=True)
    previous_active = watch.is_active

    if (
        updates.get("is_active") is True
        and watch.watched_item
        and watch.watched_item.domain_suspended
    ):
        raise HTTPException(status_code=409, detail="Domain is inactive")

    column_names = {c.key for c in Watch.__table__.columns}
    for field, value in updates.items():
        if field not in column_names:
            raise HTTPException(status_code=422, detail=f"Unknown field: {field}")
        setattr(watch, field, value)

    audit(
        session,
        EventType.WATCH_UPDATED,
        watch_id=watch.id,
        updated_fields=list(updates.keys()),
    )
    if "is_active" in updates:
        # #185 Phase A: resolve URL from local WatchedItem; no Archiver SDK call.
        wi_url = watch.watched_item and watch.watched_item.effective_url
        resolved_url = wi_url or f"watch:{watch.id}"
        if previous_active and not watch.is_active:
            await dispatch_event_notifications(
                session=session,
                event=WatchEvent(
                    event_type=WatchEventType.WATCH_PAUSED,
                    watch_id=str(watch.id),
                    watch_name=watch.name,
                    watch_url=resolved_url,
                    occurred_at=datetime.now(UTC),
                ),
            )
        elif not previous_active and watch.is_active:
            await dispatch_event_notifications(
                session=session,
                event=WatchEvent(
                    event_type=WatchEventType.WATCH_RESUMED,
                    watch_id=str(watch.id),
                    watch_name=watch.name,
                    watch_url=resolved_url,
                    occurred_at=datetime.now(UTC),
                ),
            )
    await session.commit()
    await session.refresh(watch)
    return watch


@router.delete("/{watch_id}", status_code=204)
async def delete_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Permanently delete an archived Watch.

    #160 pinned semantics: ``DELETE`` removes exactly one Watch — never cascades
    to siblings.
    """
    watch = await get_watch_or_404(watch_id, session)

    if not watch.is_archived:
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "not_archived",
                "message": "Archive watch before deleting",
            },
        )

    # #185 Phase A: resolve URL from local WatchedItem; no Archiver SDK call.
    resolved_url = (watch.watched_item and watch.watched_item.effective_url) or f"watch:{watch.id}"
    audit(
        session,
        EventType.WATCH_DELETED,
        watch_id=watch.id,
        name=watch.name,
        url=resolved_url,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_DELETED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=resolved_url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.delete(watch)
    await session.commit()


@router.post("/{watch_id}/deactivate", response_model=WatchResponse)
async def deactivate_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch without deleting it."""
    watch = await get_watch_or_404(watch_id, session)

    watch.is_active = False
    audit(
        session,
        EventType.WATCH_DEACTIVATED,
        watch_id=watch.id,
        name=watch.name,
    )
    await session.commit()
    await session.refresh(watch)
    return watch
