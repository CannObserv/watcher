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
)
from src.core.domains import domain_name_for_url, ensure_domain_and_resolve_suspension
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.change_revision import ChangeRevision
from src.core.models.watched_item import WatchedItem
from src.core.probe import ProbeResult
from src.core.registry import get_registry
from src.core.watched_items import (
    ArchivedItemActivationError,
    SuspendedDomainResumeError,
    resolve_watch_target,
    set_watched_item_active,
)
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

        # Derive the domain from the supplied URL without re-probing — Archiver is
        # authoritative for the URL. Mirrors the URL-only branch so an InfoItem-linked
        # create (the Archiver "Begin Watching" path) doesn't leave domain_name NULL (#196).
        domain_name = domain_name_for_url(data.url)
        domain_state = await ensure_domain_and_resolve_suspension(session, domain_name)
        wi = WatchedItem(
            archiver_info_item_id=ULID.from_str(data.archiver_info_item_id),
            name=data.name or info_item.name,
            description=data.description,
            is_active=data.is_active,
            default_schedule_config=data.default_schedule_config,
            content_media_type=data.content_media_type,
            default_tags=data.default_tags,
            effective_url=data.url or "",
            domain_name=domain_name,
            domain_suspended=domain_state.suspended,
            domain_default_schedule_config=domain_state.default_schedule_config,
            source_specs=data.source_specs or [],
            archiver_info_source_id=data.archiver_info_source_id,
        )
    else:
        # Mode-branched (#241): local probes inline; bus defers the probe to the
        # first fetch (item starts PROBING, resolved by apply_fetch_blob).
        try:
            effective_url, domain, health_status = await resolve_watch_target(data.url, probe_fn)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

        # #191: schedule_tick gates solely on WatchedItem.domain_suspended (no live
        # Domain join), so initialize it from the existing domain's state here —
        # otherwise an item created on an already-inactive/archived domain would be
        # scheduled. Mirrors the re-probe route's cascade (#196: shared helper).
        domain_state = await ensure_domain_and_resolve_suspension(session, domain)

        wi = WatchedItem(
            effective_url=effective_url,
            domain_name=domain,
            domain_suspended=domain_state.suspended,
            domain_default_schedule_config=domain_state.default_schedule_config,
            health_status=health_status,
            name=data.name or domain or data.url,
            description=data.description,
            is_active=data.is_active,
            default_schedule_config=data.default_schedule_config,
            content_media_type=data.content_media_type,
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

    ``is_active`` (pause/resume) is governed by the shared
    :func:`set_watched_item_active` service (#228): it cannot change on an
    archived item (409 — restore owns activation) and an item cannot resume
    while its domain is suspended (409 — kill-switch parity with the
    dashboard toggle).

    An ``is_active`` transition emits a dedicated ``WATCHED_ITEM_PAUSED`` /
    ``WATCHED_ITEM_RESUMED`` audit event (#189) and is excluded from the
    generic ``WATCHED_ITEM_UPDATED`` event, which carries only the other
    changed fields. A no-op (same value) emits nothing.
    """
    wi = await _get_or_404(session, watched_item_id)
    updates = data.model_dump(exclude_unset=True)
    # Non-None when present: the schema's _reject_explicit_null forbids
    # ``"is_active": null``, so a popped value is always a real bool.
    target_active = updates.pop("is_active", None)
    for field, value in updates.items():
        setattr(wi, field, value)

    # #196: a PATCH that sets effective_url must re-derive domain_name (no re-probe;
    # Archiver is authoritative for the URL), upsert the Domain, and re-evaluate
    # domain_suspended — otherwise the Archiver "Begin Watching" PATCH leaves the
    # item unassociated from its Domain (kill-switch + domain notifications miss it).
    if "effective_url" in updates:
        derived_domain = domain_name_for_url(wi.effective_url)
        # Upsert the Domain before assigning wi.domain_name so the helper's internal
        # SELECT (which autoflushes the dirty WatchedItem) can't trip the FK.
        domain_state = await ensure_domain_and_resolve_suspension(session, derived_domain)
        wi.domain_name = derived_domain
        wi.domain_suspended = domain_state.suspended
        wi.domain_default_schedule_config = domain_state.default_schedule_config

    # #228: the pause/resume transition (guards + dedicated audit event) is
    # owned by the shared service; runs after the effective_url block so the
    # resume guard sees the re-derived domain_suspended state.
    if target_active is not None:
        try:
            set_watched_item_active(session, wi, active=target_active, source="api")
        except (ArchivedItemActivationError, SuspendedDomainResumeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    other_fields = sorted(updates)
    # domain_name is derived (not a PATCH input) but changes with effective_url;
    # surface it in the audit so the trail matches the re-probe route (#196).
    if "effective_url" in updates:
        other_fields = sorted([*other_fields, "domain_name"])
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


@router.delete("/{watched_item_id}", status_code=204)
async def delete_watched_item(
    watched_item_id: str, session: AsyncSession = Depends(get_db_session)
):
    """Permanently delete an archived WatchedItem (#210).

    Pre-flight: 404 if not found / malformed id; 409 if the item is not archived
    (archive first — archived already implies ``is_active=False``). On success the
    DB cascades the item's children (``temporal_profiles``,
    ``notification_templates``, ``change_revisions``, ``pending_archiver_sync``)
    via their ``ON DELETE CASCADE`` FKs. An audit row is written before the delete
    and survives it (the WatchedItem id lives in the JSONB payload, not an FK).
    Archiver-side content (InfoItem / SourceRevisions) is left untouched.
    """
    wi = await _get_or_404(session, watched_item_id)

    if wi.archived_at is None:
        raise HTTPException(status_code=409, detail="WatchedItem must be archived before deletion")

    audit(
        session,
        EventType.WATCHED_ITEM_DELETED,
        watched_item_id=str(wi.id),
        name=wi.name,
        url=wi.effective_url,
        source="api",
    )
    await session.delete(wi)
    await session.commit()


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
