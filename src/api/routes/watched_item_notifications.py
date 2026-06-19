"""Per-WatchedItem notification API — item-scoped templates + effective set (#200).

This nested surface (``/watched-items/{id}/notifications``) is the convenience
entry point for managing a single item's ``visibility='watched_item'`` templates,
and the one place to answer "which channels fire for this item?" via
``GET .../effective`` (global + the item's domain + the item itself).

Post-#200 there is no separate "config" object — every target is a
``NotificationTemplate`` row. After Phase 5 (#137) templates are pure
remote-channel pointers; the notifier service owns delivery.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from notifier_client.errors import NotifierError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.deps import get_db_session
from src.api.routes.helpers import get_watched_item_or_404, parse_ulid
from src.api.schemas.notification_template import (
    ItemNotificationTemplateCreate,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import DispatchCandidate, dispatch_via_notifier
from src.core.notifier_client import get_notifier_client

logger = get_logger(__name__)

router = APIRouter(
    prefix="/watched-items/{watched_item_id}/notifications", tags=["watched-item-notifications"]
)


async def _item_template_or_404(
    session: AsyncSession, watched_item_id: ULID, template_id: str
) -> NotificationTemplate:
    """Fetch a watched-item-scoped template, raising 404 if absent or not on this item."""
    tpl = await session.get(NotificationTemplate, parse_ulid(template_id, "Template"))
    if (
        tpl is None
        or tpl.visibility != VISIBILITY_WATCHED_ITEM
        or tpl.watched_item_id != watched_item_id
    ):
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.post("", status_code=201, response_model=NotificationTemplateResponse)
async def create_item_notification(
    watched_item_id: str,
    data: ItemNotificationTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplate:
    """Create a watched-item-scoped notification template."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    tpl = NotificationTemplate(
        visibility=VISIBILITY_WATCHED_ITEM,
        watched_item_id=wi.id,
        title=data.title,
        channel_hint=data.channel_hint,
        events=data.events,
        content_config=data.content_config.model_dump() if data.content_config else None,
        remote_channel_id=data.remote_channel_id,
    )
    session.add(tpl)
    await session.flush()
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        channel_hint=tpl.channel_hint,
    )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.get("", response_model=list[NotificationTemplateResponse])
async def list_item_notifications(
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[NotificationTemplate]:
    """List the watched-item-scoped templates for a WatchedItem."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.visibility == VISIBILITY_WATCHED_ITEM,
            NotificationTemplate.watched_item_id == wi.id,
        )
        .order_by(NotificationTemplate.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/effective", response_model=list[NotificationTemplateResponse])
async def list_effective_notifications(
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[NotificationTemplate]:
    """The full set of templates in scope for this item — the single F5 surface.

    Returns every template whose visibility matches the item (global, the item's
    domain, and the item itself), regardless of ``is_active`` so the caller can
    show the complete picture. The per-event ``events`` filter and ``is_active``
    still gate actual dispatch (see ``dispatch_event_notifications``).
    """
    wi = await get_watched_item_or_404(watched_item_id, session)
    clauses = [
        NotificationTemplate.visibility == VISIBILITY_GLOBAL,
        and_(
            NotificationTemplate.visibility == VISIBILITY_WATCHED_ITEM,
            NotificationTemplate.watched_item_id == wi.id,
        ),
    ]
    if wi.domain_name:
        clauses.append(
            and_(
                NotificationTemplate.visibility == VISIBILITY_DOMAIN,
                NotificationTemplate.domain_name == wi.domain_name,
            )
        )
    result = await session.execute(
        select(NotificationTemplate)
        .where(or_(*clauses))
        .order_by(NotificationTemplate.visibility, NotificationTemplate.title)
    )
    return list(result.scalars().all())


@router.patch("/{template_id}", response_model=NotificationTemplateResponse)
async def update_item_notification(
    watched_item_id: str,
    template_id: str,
    data: NotificationTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplate:
    """Update an item-scoped template's mutable fields."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    tpl = await _item_template_or_404(session, wi.id, template_id)
    if data.is_active is not None:
        tpl.is_active = data.is_active
    if data.events is not None:
        tpl.events = data.events
    if "channel_hint" in data.model_fields_set and data.channel_hint is not None:
        tpl.channel_hint = data.channel_hint
    if "remote_channel_id" in data.model_fields_set and data.remote_channel_id is not None:
        tpl.remote_channel_id = data.remote_channel_id
    if "title" in data.model_fields_set and data.title is not None:
        tpl.title = data.title
    if "content_config" in data.model_fields_set:
        tpl.content_config = data.content_config.model_dump() if data.content_config else None
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
    )
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=204)
async def delete_item_notification(
    watched_item_id: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete an item-scoped template."""
    wi = await get_watched_item_or_404(watched_item_id, session)
    tpl = await _item_template_or_404(session, wi.id, template_id)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_DELETED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
    )
    await session.delete(tpl)
    await session.commit()


@router.post("/{template_id}/test")
async def test_item_notification(
    watched_item_id: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Send a test notification for an item-scoped template via the notifier service.

    Returns {success, reason}, never 5xx.
    """
    wi = await get_watched_item_or_404(watched_item_id, session)
    tpl = await _item_template_or_404(session, wi.id, template_id)
    success = False
    reason = "Internal error during dispatch"
    try:
        resolved_url = wi.effective_url or f"watched-item:{wi.id}"
        if not tpl.remote_channel_id:
            reason = "no remote_channel_id configured"
        else:
            event = WatchEvent(
                event_type=WatchEventType.CHANGE_DETECTED,
                watched_item_id=str(wi.id),
                item_name=wi.name,
                item_url=resolved_url,
                occurred_at=datetime.now(UTC),
                metadata={"test": True},
            )
            candidate = DispatchCandidate(
                source=tpl.visibility,
                source_id=str(tpl.id),
                content_config=tpl.content_config,
                remote_channel_id=tpl.remote_channel_id,
            )
            try:
                async with get_notifier_client() as client:
                    outcome = await dispatch_via_notifier(
                        client,
                        candidate,
                        event,
                        rendered_title="[Test] WatchedItem notification",
                        rendered_body=f"Test from '{wi.name}'.",
                    )
                success = outcome.success
                reason = outcome.reason
            except NotifierError as exc:
                reason = f"notifier error: {exc}"
    except Exception:
        logger.exception("test notification error", extra={"template_id": template_id})
    audit(
        session,
        EventType.NOTIFICATION_TEST,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        channel_hint=tpl.channel_hint,
        success=success,
        reason=reason,
    )
    await session.commit()
    return {"success": success, "reason": reason}
