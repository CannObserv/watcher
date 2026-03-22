"""Watch CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.models.audit_log import AuditLog
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/watches", tags=["watches"])


@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new watch."""
    watch = Watch(
        name=data.name,
        url=data.url,
        content_type=data.content_type,
        fetch_config=data.fetch_config,
        schedule_config=data.schedule_config,
    )
    session.add(watch)
    audit = AuditLog(
        event_type="watch.created",
        watch_id=watch.id,
        payload={"name": data.name, "url": data.url, "content_type": data.content_type.value},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.get("", response_model=list[WatchResponse])
async def list_watches(
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """List all watches, optionally filtered by active status."""
    stmt = select(Watch).order_by(Watch.created_at.desc())
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
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
    column_names = {c.key for c in Watch.__table__.columns}
    for field, value in updates.items():
        if field not in column_names:
            raise HTTPException(status_code=422, detail=f"Unknown field: {field}")
        setattr(watch, field, value)

    audit = AuditLog(
        event_type="watch.updated",
        watch_id=watch.id,
        payload={"updated_fields": list(updates.keys())},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.delete("/{watch_id}", status_code=204, response_class=Response)
async def delete_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Permanently delete an inactive watch and all related data."""
    watch = await get_watch_or_404(watch_id, session)

    if watch.is_active:
        raise HTTPException(status_code=409, detail="Deactivate watch before deleting")

    audit = AuditLog(
        event_type="watch.deleted",
        watch_id=watch.id,
        payload={"name": watch.name, "url": watch.url},
    )
    session.add(audit)
    await session.delete(watch)
    await session.commit()
    return Response(status_code=204)


@router.post("/{watch_id}/deactivate", response_model=WatchResponse)
async def deactivate_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch without deleting it."""
    watch = await get_watch_or_404(watch_id, session)

    watch.is_active = False
    audit = AuditLog(
        event_type="watch.deactivated",
        watch_id=watch.id,
        payload={"name": watch.name},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch
