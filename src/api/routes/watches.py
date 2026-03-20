"""Watch CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.models.audit_log import AuditLog
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/watches", tags=["watches"])


def _parse_ulid(watch_id: str) -> ULID:
    """Parse a ULID string, raising 404 on invalid format."""
    try:
        return ULID.from_str(watch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Watch not found") from exc


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
    watch = await session.get(Watch, _parse_ulid(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    return watch


@router.patch("/{watch_id}", response_model=WatchResponse)
async def update_watch(
    watch_id: str,
    data: WatchUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update a watch. Only provided fields are changed."""
    watch = await session.get(Watch, _parse_ulid(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
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


@router.post("/{watch_id}/deactivate", response_model=WatchResponse)
async def deactivate_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch without deleting it."""
    watch = await session.get(Watch, _parse_ulid(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

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
