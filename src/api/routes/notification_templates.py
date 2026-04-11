"""CRUD API for shared notification templates."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404
from src.api.schemas.notification_config import extract_channel_hint
from src.api.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from src.core.crypto import encrypt_apprise_url
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType

router = APIRouter(prefix="/notifications/templates", tags=["notification-templates"])
logger = get_logger(__name__)


async def _get_template_or_404(template_id: str, session: AsyncSession) -> NotificationTemplate:
    """Fetch a NotificationTemplate by id or raise 404."""
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)  # type: ignore[arg-type]
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


async def _ref_counts(tpl: NotificationTemplate, session: AsyncSession) -> tuple[int, int]:
    """Return (watch_count, domain_count) for a template."""
    watch_count = (
        await session.scalar(
            select(func.count()).where(WatchNcRef.template_id == tpl.id)  # type: ignore[arg-type]
        )
        or 0
    )
    domain_count = (
        await session.scalar(
            select(func.count()).where(DomainNcRef.template_id == tpl.id)  # type: ignore[arg-type]
        )
        or 0
    )
    return watch_count, domain_count


@router.post("", status_code=201, response_model=NotificationTemplateResponse)
async def create_template(
    data: NotificationTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    """Create a new shared notification template."""
    tpl = NotificationTemplate(
        title=data.title,
        apprise_url=encrypt_apprise_url(data.apprise_url),
        channel_hint=extract_channel_hint(data.apprise_url),
        events=data.events,
        is_global_default=data.is_global_default,
    )
    session.add(tpl)
    await session.flush()
    audit(session, EventType.NOTIFICATION_TEMPLATE_CREATED, template_id=str(tpl.id))
    await session.commit()
    return NotificationTemplateResponse(**tpl.__dict__, watch_ref_count=0, domain_ref_count=0)


@router.get("", response_model=list[NotificationTemplateResponse])
async def list_templates(
    session: AsyncSession = Depends(get_db_session),
) -> list[NotificationTemplateResponse]:
    """List all notification templates ordered by title."""
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    watch_counts_result = await session.execute(
        select(WatchNcRef.template_id, func.count().label("cnt")).group_by(WatchNcRef.template_id)
    )
    watch_counts = {str(row.template_id): row.cnt for row in watch_counts_result}
    domain_counts_result = await session.execute(
        select(DomainNcRef.template_id, func.count().label("cnt")).group_by(DomainNcRef.template_id)
    )
    domain_counts = {str(row.template_id): row.cnt for row in domain_counts_result}
    return [
        NotificationTemplateResponse(
            **tpl.__dict__,
            watch_ref_count=watch_counts.get(str(tpl.id), 0),
            domain_ref_count=domain_counts.get(str(tpl.id), 0),
        )
        for tpl in notification_templates
    ]


@router.get("/{template_id}", response_model=NotificationTemplateResponse)
async def get_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    """Fetch a single notification template by id."""
    tpl = await _get_template_or_404(template_id, session)
    watch_count, domain_count = await _ref_counts(tpl, session)
    return NotificationTemplateResponse(
        **tpl.__dict__, watch_ref_count=watch_count, domain_ref_count=domain_count
    )


@router.patch("/{template_id}", response_model=NotificationTemplateResponse)
async def update_template(
    template_id: str,
    data: NotificationTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplateResponse:
    """Partially update a notification template."""
    tpl = await _get_template_or_404(template_id, session)
    if data.apprise_url is not None:
        tpl.apprise_url = encrypt_apprise_url(data.apprise_url)
        tpl.channel_hint = extract_channel_hint(data.apprise_url)
    if data.events is not None:
        tpl.events = data.events
    if data.is_global_default is not None:
        tpl.is_global_default = data.is_global_default
    if data.is_active is not None:
        tpl.is_active = data.is_active
    if "title" in data.model_fields_set and data.title is not None:
        tpl.title = data.title
    audit(session, EventType.NOTIFICATION_TEMPLATE_UPDATED, template_id=str(tpl.id))
    await session.commit()
    watch_count, domain_count = await _ref_counts(tpl, session)
    return NotificationTemplateResponse(
        **tpl.__dict__, watch_ref_count=watch_count, domain_ref_count=domain_count
    )


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a template. Returns 409 if any watch or domain references it."""
    tpl = await _get_template_or_404(template_id, session)
    watch_count, domain_count = await _ref_counts(tpl, session)
    if watch_count or domain_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Template is referenced by {watch_count} watch(es) and "
                f"{domain_count} domain(s). Unassign all references first."
            ),
        )
    audit(session, EventType.NOTIFICATION_TEMPLATE_DELETED, template_id=template_id)
    await session.delete(tpl)
    await session.commit()


@router.post("/{template_id}/assign/{watch_id}", status_code=201)
async def assign_template_to_watch(
    template_id: str,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Assign a notification template to a watch (idempotent)."""
    tpl = await _get_template_or_404(template_id, session)
    watch = await get_watch_or_404(watch_id, session)
    existing = await session.scalar(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch.id,
            WatchNcRef.template_id == tpl.id,
        )
    )
    if not existing:
        session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        audit(session, EventType.WATCH_NC_ASSIGNED, watch_id=watch_id, template_id=template_id)
        await session.commit()
    return {"assigned": True}


@router.delete("/{template_id}/assign/{watch_id}", status_code=204)
async def unassign_template_from_watch(
    template_id: str,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Unassign a notification template from a watch."""
    watch = await get_watch_or_404(watch_id, session)
    result = await session.execute(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch.id,
            WatchNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    ref = result.scalar_one_or_none()
    if ref:
        await session.delete(ref)
        audit(session, EventType.WATCH_NC_UNASSIGNED, watch_id=watch_id, template_id=template_id)
        await session.commit()


@router.post("/{template_id}/test")
async def test_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Send a test notification using this template's configured URL."""
    tpl = await _get_template_or_404(template_id, session)
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id="00000000000000000000000000",
        watch_name="[Test]",
        watch_url="https://example.com",
        occurred_at=datetime.now(UTC),
    )
    try:
        result = await dispatch_event(event, tpl.apprise_url)
        audit(session, EventType.NOTIFICATION_TEMPLATE_TESTED, template_id=template_id)
        await session.commit()
        return {"success": result.success, "reason": result.reason}
    except Exception as exc:
        return {"success": False, "reason": str(exc)}
