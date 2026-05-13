"""Watch CRUD API endpoints."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

import httpx
from archiver_client import AuthError, NotFound, ServerError
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, get_probe_fn
from src.api.routes.helpers import get_watch_or_404
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult
from src.core.registry import get_registry
from src.core.watches import create_watch as _create_watch
from src.core.watches import resolve_watch_url
from src.core.watches.invariants import (
    FragmentDependentsExistError,
    RootWatchMissingError,
    _get_fragment_watch_dependents,
    require_no_fragment_dependents,
    require_root_watch_on_chain,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/watches", tags=["watches"])


@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    probe_fn: Annotated[Callable[[str], Awaitable[ProbeResult]], Depends(get_probe_fn)],
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new watch bound to an existing InfoItem.

    The handler validates ``info_item_id`` via the ArchiverClient SDK,
    probes the resolved URL to populate ``effective_*`` fields, upserts the
    Domain row, and persists the Watch.

    Error mapping:
    - SDK ``NotFound`` (unknown ``info_item_id``) → 422.
    - SDK ``AuthError`` → 500 (operator misconfiguration).
    - SDK ``ServerError`` / ``httpx.ConnectError`` / ``httpx.TimeoutException``
      → 503 with ``Retry-After: 30`` header.
    - URL probe ``httpx.HTTPError`` → 422 (target unreachable).
    """
    info_client = get_registry().get_archiver_client()

    # Fragment-root invariant: if info_source_id is supplied and is a fragment
    # (parent is not None), require an active root Watch on the chain.
    if data.info_source_id is not None:
        try:
            source = await info_client.get_info_source(data.info_source_id)
            if source.parent_info_source_id is not None:
                await require_root_watch_on_chain(
                    session, info_client, info_source_id=data.info_source_id
                )
        except RootWatchMissingError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "kind": "domain",
                    "message": "fragment requires active root Watch",
                    "info_source_id": data.info_source_id,
                },
            ) from exc
        except AuthError:
            logger.exception("ArchiverClient auth failure during fragment-root check")
            raise HTTPException(status_code=500, detail="Information service auth failed") from None
        except (ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("Information service unreachable during fragment-root check: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Information service unavailable; retry shortly",
                headers={"Retry-After": "30"},
            ) from exc

    try:
        return await _create_watch(
            session=session,
            probe_fn=probe_fn,
            info_client=info_client,
            name=data.name,
            info_item_id=data.info_item_id,
            content_type=data.content_type,
            schedule_config=data.schedule_config,
            description=data.description,
            tags=data.tags,
            info_source_id=data.info_source_id,
        )
    except NotFound as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"info_item_id {data.info_item_id} or "
                f"info_source_id {data.info_source_id} does not exist"
            ),
        ) from exc
    except AuthError:
        # Loud — operator-fixable misconfiguration of ARCHIVER_API_KEY.
        logger.exception("ArchiverClient auth failure during watch create")
        raise HTTPException(status_code=500, detail="Information service auth failed") from None
    except (ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("Information service unreachable during watch create: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Information service unavailable; retry shortly",
            headers={"Retry-After": "30"},
        ) from exc
    except httpx.HTTPError as exc:
        # URL probe failure — the InfoSpec URL is unreachable.
        raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc


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
        info_client = get_registry().get_archiver_client()
        try:
            resolved_url = await resolve_watch_url(watch, info_client)
        except NotFound:
            # Mirrors src/workers/tasks.py: an InfoItem deleted out from under
            # the watch should not block operator-requested pause/resume.
            # Fall back to a sentinel URL so the notification still fires.
            logger.error(
                "info_item missing for watch — emitting lifecycle event with sentinel URL",
                extra={"watch_id": str(watch.id)},
            )
            resolved_url = f"watch:{watch.id}"
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
    cascade: bool = Query(False),
    session: AsyncSession = Depends(get_db_session),
):
    """Permanently delete an archived watch and all related data.

    If the watch is a root (its info_source has no parent), fragment Watches
    that depend on it must be handled first:
    - Without ``?cascade=true``: returns 409 with a list of dependent Watches.
    - With ``?cascade=true``: archives each fragment Watch in the same
      transaction before proceeding with the deletion.
    """
    watch = await get_watch_or_404(watch_id, session)

    if not watch.is_archived:
        raise HTTPException(status_code=409, detail="Archive watch before deleting")

    info_client = get_registry().get_archiver_client()

    # Fragment-dependents check: only applies when the Watch has an info_source_id
    # (v2 watches). Root Watches block deletion when active fragment Watches exist.
    if watch.info_source_id is not None:
        try:
            source = await info_client.get_info_source(str(watch.info_source_id))
            is_root = source.parent_info_source_id is None
            if is_root:
                if cascade:
                    # Archive all non-archived fragment Watches in this transaction.
                    dependents = await _get_fragment_watch_dependents(session, info_client, watch)
                    for dep_watch in dependents:
                        dep_watch.is_archived = True
                        dep_watch.is_active = False
                    if dependents:
                        await session.flush()
                else:
                    await require_no_fragment_dependents(session, info_client, watch)
        except FragmentDependentsExistError as exc:
            # Dependents already carried on the exception; no second Archiver call needed.
            dependents = exc.dependents
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "conflict",
                    "message": "fragment Watches depend on this root Watch",
                    "data": {
                        "dependents": [
                            {
                                "watch_id": str(dep.id),
                                "info_source_id": str(dep.info_source_id),
                            }
                            for dep in dependents
                        ]
                    },
                },
            ) from exc
        except (NotFound, ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "Information service unreachable during fragment-dependents check: %s", exc
            )
            # Fail open: don't block deletion if we can't reach the archiver.

    try:
        resolved_url = await resolve_watch_url(watch, info_client)
    except NotFound:
        # Mirrors src/workers/tasks.py: an InfoItem deleted out from under the
        # watch should not block operator-requested deletion. Fall back to a
        # sentinel URL so audit + notification still record the event.
        logger.error(
            "info_item missing for watch — deleting with sentinel URL",
            extra={"watch_id": str(watch.id)},
        )
        resolved_url = f"watch:{watch.id}"
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
