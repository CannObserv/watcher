"""Watch CRUD API endpoints."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session, get_probe_fn
from src.api.routes.helpers import get_watch_or_404
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.workers.notify import dispatch_event_notifications

logger = get_logger(__name__)

router = APIRouter(prefix="/watches", tags=["watches"])


async def _assign_default_templates(session: AsyncSession, watch: Watch) -> None:
    """Assign global and domain NC defaults to a newly created watch. Non-fatal."""
    try:
        template_ids: set = set()

        # Global defaults — assigned regardless of is_active; inactivity is temporary
        global_result = await session.execute(
            select(NotificationTemplate.id).where(
                NotificationTemplate.is_global_default.is_(True),
            )
        )
        template_ids.update(row[0] for row in global_result)

        # Domain defaults
        if watch.effective_domain:
            domain_result = await session.execute(
                select(DomainNcRef.template_id).where(
                    DomainNcRef.domain_name == watch.effective_domain
                )
            )
            template_ids.update(row[0] for row in domain_result)

        for template_id in template_ids:
            session.add(WatchNcRef(watch_id=watch.id, template_id=template_id))

        if template_ids:
            await session.flush()

    except Exception:
        logger.warning("Failed to assign default NC templates to watch", watch_id=str(watch.id))


@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    probe_fn: Annotated[Callable[[str], Awaitable], Depends(get_probe_fn)],
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new watch. Probes the URL to resolve effective domain.

    The probe fails fast on connection errors (httpx.HTTPError). Non-2xx HTTP
    responses (e.g. 404, 500) are treated as reachable — the watch is still
    created so monitoring can begin and detect when the URL becomes healthy.
    """
    try:
        probe_result = await probe_fn(data.url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

    # Upsert domain — insert with defaults if new, leave config intact if exists.
    # Guard against TOCTOU race: concurrent requests may both pass the
    # scalar_one_or_none() check and hit the unique constraint simultaneously.
    domain_stmt = select(Domain).where(Domain.name == probe_result.effective_domain)
    domain_result = await session.execute(domain_stmt)
    if not domain_result.scalar_one_or_none():
        try:
            session.add(
                Domain(
                    name=probe_result.effective_domain,
                    min_interval=DEFAULT_MIN_INTERVAL,
                    max_concurrency=DEFAULT_MAX_CONCURRENCY,
                    current_interval=DEFAULT_MIN_INTERVAL,
                )
            )
            await session.flush()
        except IntegrityError:
            await session.rollback()

    watch = Watch(
        name=data.name,
        url=data.url,
        content_type=data.content_type,
        fetch_config=data.fetch_config,
        schedule_config=data.schedule_config,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    session.add(watch)
    await session.flush()
    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=data.name,
        url=data.url,
        content_type=data.content_type.value,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_CREATED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=watch.url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await _assign_default_templates(session, watch)
    await session.commit()
    await session.refresh(watch)
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

    if updates.get("is_active") is True and watch.effective_domain:
        domain_result = await session.execute(
            select(Domain).where(Domain.name == watch.effective_domain)
        )
        domain = domain_result.scalar_one_or_none()
        if domain and not domain.is_active:
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
        if previous_active and not watch.is_active:
            await dispatch_event_notifications(
                session=session,
                event=WatchEvent(
                    event_type=WatchEventType.WATCH_PAUSED,
                    watch_id=str(watch.id),
                    watch_name=watch.name,
                    watch_url=watch.url,
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
                    watch_url=watch.url,
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
    """Permanently delete an archived watch and all related data."""
    watch = await get_watch_or_404(watch_id, session)

    if not watch.is_archived:
        raise HTTPException(status_code=409, detail="Archive watch before deleting")

    audit(
        session,
        EventType.WATCH_DELETED,
        watch_id=watch.id,
        name=watch.name,
        url=watch.url,
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
