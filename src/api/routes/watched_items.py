"""WatchedItem CRUD API endpoints (#161, #185 Phase A)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from archiver_client import AuthError, NotFound, ServerError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.deps import get_db_session, get_probe_fn
from src.api.routes.helpers import parse_filter_ulid, parse_ulid
from src.api.schemas.watched_item import (
    ChangeRevisionResponse,
    WatchedItemCreate,
    WatchedItemPatch,
    WatchedItemResponse,
    WatchedItemTemplateCreate,
    WatchedItemTemplatePatch,
    WatchedItemTemplateResponse,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.change_revision import ChangeRevision
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watched_item import WatchedItem
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)
from src.core.probe import ProbeResult
from src.core.registry import get_registry
from src.workers.tasks import check_watched_item

router = APIRouter(prefix="/watched-items", tags=["watched-items"])

logger = get_logger(__name__)


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
    domain: str | None = None,
    archiver_info_item_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """List WatchedItems. Archived excluded unless ``include_archived=true``.
    Filter by domain hostname with ``domain=`` or by Archiver InfoItem with
    ``archiver_info_item_id=``."""
    stmt = select(WatchedItem).order_by(WatchedItem.name)
    if not include_archived:
        stmt = stmt.where(WatchedItem.archived_at.is_(None))
    if domain is not None:
        stmt = stmt.where(WatchedItem.domain_name == domain)
    if archiver_info_item_id is not None:
        ulid = parse_filter_ulid(archiver_info_item_id, "archiver_info_item_id")
        stmt = stmt.where(WatchedItem.archiver_info_item_id == ulid)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.post("", response_model=WatchedItemResponse, status_code=201)
async def create_watched_item(
    data: WatchedItemCreate,
    session: AsyncSession = Depends(get_db_session),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
):
    """Create a standalone WatchedItem.

    Two paths depending on which anchor is provided:

    **InfoItem-linked** (``archiver_info_item_id`` set): validates the InfoItem via the
    Archiver SDK; name defaults to the InfoItem's name.
    Errors: NotFound → 422, AuthError → 500, ServerError/network → 503.

    **URL-only** (``url`` set, no ``archiver_info_item_id``): probes the URL for
    ``effective_url`` + ``domain_name``; name defaults to the probed domain.
    ``archiver_info_item_id`` is null on the resulting record.
    Error: unreachable URL → 422.

    At least one of ``archiver_info_item_id`` or ``url`` is required (schema-enforced).
    """
    if data.archiver_info_item_id:
        info_client = get_registry().get_archiver_client()
        try:
            info_item = await info_client.get_info_item(data.archiver_info_item_id)
        except NotFound as exc:
            raise HTTPException(
                status_code=422,
                detail=f"archiver_info_item_id {data.archiver_info_item_id} does not exist",
            ) from exc
        except AuthError:
            logger.exception("ArchiverClient auth failure during watched_item create")
            raise HTTPException(status_code=500, detail="Information service auth failed") from None
        except (ServerError, httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("Information service unreachable during watched_item create: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Information service unavailable; retry shortly",
                headers={"Retry-After": "30"},
            ) from exc

        wi = WatchedItem(
            archiver_info_item_id=ULID.from_str(data.archiver_info_item_id),
            name=data.name or info_item.name,
            description=data.description,
            is_active=data.is_active,
            default_schedule_config=data.default_schedule_config,
            default_content_type=data.default_content_type,
            default_tags=data.default_tags,
            effective_url=data.url or "",
            source_specs=data.source_specs or [],
            archiver_info_source_id=data.archiver_info_source_id,
        )
    else:
        try:
            probe_result = await probe_fn(data.url)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

        domain = probe_result.effective_domain
        if domain:
            if not (
                await session.execute(select(Domain).where(Domain.name == domain))
            ).scalar_one_or_none():
                try:
                    async with session.begin_nested():
                        session.add(
                            Domain(
                                name=domain,
                                min_interval=DEFAULT_MIN_INTERVAL,
                                max_concurrency=DEFAULT_MAX_CONCURRENCY,
                                current_interval=DEFAULT_MIN_INTERVAL,
                            )
                        )
                except IntegrityError:
                    pass

        wi = WatchedItem(
            effective_url=probe_result.effective_url,
            domain_name=domain or None,
            name=data.name or domain or data.url,
            description=data.description,
            is_active=data.is_active,
            default_schedule_config=data.default_schedule_config,
            default_content_type=data.default_content_type,
            default_tags=data.default_tags,
            source_specs=data.source_specs or [],
            archiver_info_source_id=data.archiver_info_source_id,
        )

    session.add(wi)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"WatchedItem for archiver_info_item_id {data.archiver_info_item_id} already exists"
            ),
        ) from exc
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        archiver_info_item_id=data.archiver_info_item_id,
        name=wi.name,
        source="api",
    )
    await session.commit()
    await session.refresh(wi)
    return wi


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
    """Update mutable WatchedItem fields. All fields optional.

    ``is_active`` (pause/resume) cannot be changed on an archived item — the
    archive/restore lifecycle owns activation while archived. Such a PATCH
    returns 409; use ``POST /{id}/restore`` to reactivate.

    An ``is_active`` transition emits a dedicated ``WATCHED_ITEM_PAUSED`` /
    ``WATCHED_ITEM_RESUMED`` audit event (#189) and is excluded from the
    generic ``WATCHED_ITEM_UPDATED`` event, which carries only the other
    changed fields. A no-op (same value) emits nothing.
    """
    wi = await _get_or_404(session, watched_item_id)
    updates = data.model_dump(exclude_unset=True)
    if "is_active" in updates and wi.archived_at is not None:
        raise HTTPException(
            status_code=409,
            detail="WatchedItem is archived; activation is controlled by restore",
        )
    previous_active = wi.is_active
    for field, value in updates.items():
        setattr(wi, field, value)

    # #189: an is_active transition gets a dedicated pause/resume audit event
    # (mirroring archive/restore), kept out of the generic UPDATED entry so
    # operators can filter by event type. A no-op (same value) emits nothing.
    if "is_active" in updates and wi.is_active != previous_active:
        audit(
            session,
            EventType.WATCHED_ITEM_RESUMED if wi.is_active else EventType.WATCHED_ITEM_PAUSED,
            watched_item_id=str(wi.id),
            source="api",
        )

    other_fields = sorted(k for k in updates if k != "is_active")
    if other_fields:
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=other_fields,
            source="api",
        )
    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/archive", response_model=WatchedItemResponse)
async def archive_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Archive a WatchedItem (the single monitored entity, #191).

    Sets ``archived_at`` and flips ``is_active`` to False; the fetch cycle stops
    within one ``schedule_tick`` interval because the tick filters on
    ``WatchedItem.archived_at IS NULL``.
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

    await session.commit()
    await session.refresh(wi)
    return wi


@router.post("/{watched_item_id}/restore", response_model=WatchedItemResponse)
async def restore_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Restore the WatchedItem — clears ``archived_at`` and re-activates."""
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


@router.post("/{watched_item_id}/check-now", response_model=WatchedItemResponse, status_code=202)
async def check_now(watched_item_id: str, session: AsyncSession = Depends(get_db_session)):
    """Enqueue an immediate ``check_watched_item`` task for a WatchedItem.

    Pre-flight guards:
    - 409 if the WatchedItem is archived.
    - 409 if the WatchedItem is paused (``is_active=False``) — the task would
      short-circuit, so reject up front rather than enqueue a silent no-op.
    - 422 if ``effective_url`` is empty (nothing to fetch).
    """
    wi = await _get_or_404(session, watched_item_id)

    if wi.archived_at is not None:
        raise HTTPException(status_code=409, detail="WatchedItem is archived")

    if not wi.is_active:
        raise HTTPException(status_code=409, detail="WatchedItem is paused")

    if not wi.effective_url:
        raise HTTPException(status_code=422, detail="WatchedItem has no effective url")

    await check_watched_item.configure().defer_async(watched_item_id=str(wi.id))
    audit(session, EventType.WATCHED_ITEM_CHECK_REQUESTED, watched_item_id=str(wi.id), source="api")
    await session.commit()
    return wi


@router.get(
    "/{watched_item_id}/revisions",
    response_model=list[ChangeRevisionResponse],
)
async def list_revisions(watched_item_id: str, session: AsyncSession = Depends(get_db_session)):
    """List ChangeRevisions for a WatchedItem, newest first."""
    wi = await _get_or_404(session, watched_item_id)
    result = await session.execute(
        select(ChangeRevision)
        .where(ChangeRevision.watched_item_id == wi.id)
        .order_by(ChangeRevision.captured_at.desc())
    )
    return list(result.scalars().all())


async def _template_or_404(
    session: AsyncSession, wi: WatchedItem, tpl_id: str
) -> WatchedItemNotificationTemplate:
    """Fetch a WatchedItemNotificationTemplate, raising 404 if absent or mismatched."""
    tpl = await session.get(WatchedItemNotificationTemplate, parse_ulid(tpl_id))
    if tpl is None or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.get(
    "/{watched_item_id}/notification-templates",
    response_model=list[WatchedItemTemplateResponse],
)
async def list_templates(watched_item_id: str, session: AsyncSession = Depends(get_db_session)):
    """List notification templates under a WatchedItem."""
    wi = await _get_or_404(session, watched_item_id)
    result = await session.execute(
        select(WatchedItemNotificationTemplate)
        .where(WatchedItemNotificationTemplate.watched_item_id == wi.id)
        .order_by(WatchedItemNotificationTemplate.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "/{watched_item_id}/notification-templates",
    response_model=WatchedItemTemplateResponse,
    status_code=201,
)
async def create_template(
    watched_item_id: str,
    data: WatchedItemTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification template under a WatchedItem."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = WatchedItemNotificationTemplate(
        watched_item_id=wi.id,
        **data.model_dump(),
    )
    session.add(tpl)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_CREATED,
        watched_item_id=str(wi.id),
        source="api",
    )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.patch(
    "/{watched_item_id}/notification-templates/{tpl_id}",
    response_model=WatchedItemTemplateResponse,
)
async def patch_template(
    watched_item_id: str,
    tpl_id: str,
    data: WatchedItemTemplatePatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Update fields on an existing template."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = await _template_or_404(session, wi, tpl_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(tpl, field, value)
    if updates:
        audit(
            session,
            EventType.WATCHED_ITEM_TEMPLATE_UPDATED,
            watched_item_id=str(wi.id),
            template_id=str(tpl.id),
            updated_fields=sorted(updates.keys()),
            source="api",
        )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete("/{watched_item_id}/notification-templates/{tpl_id}", status_code=204)
async def delete_template(
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a template."""
    wi = await _get_or_404(session, watched_item_id)
    tpl = await _template_or_404(session, wi, tpl_id)
    audit(
        session,
        EventType.WATCHED_ITEM_TEMPLATE_DELETED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="api",
    )
    await session.delete(tpl)
    await session.commit()
