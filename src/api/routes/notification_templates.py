"""CRUD API for notification templates at any visibility scope (#200).

Post-#200 a template carries an intrinsic ``visibility`` (global / domain /
watched_item); there are no junction tables. This route is the generic,
scope-agnostic surface; the per-item convenience surface lives under
``/watched-items/{id}/notifications``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from notifier_client.errors import NotifierError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.deps import get_db_session
from src.api.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import NotificationTemplate
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import DispatchCandidate, dispatch_via_notifier
from src.core.notifier_client import get_notifier_client

router = APIRouter(prefix="/notifications/templates", tags=["notification-templates"])
logger = get_logger(__name__)

# Sentinel watched_item_id for "[Test]" dispatch events that aren't tied to a
# real WatchedItem. ``ULID.from_int(0)`` renders as 26 zero-base32 chars and
# stays inside the strict 26-char ULID validation the schemas enforce.
_TEST_SENTINEL_WATCHED_ITEM_ID = str(ULID.from_int(0))


async def _get_template_or_404(template_id: str, session: AsyncSession) -> NotificationTemplate:
    """Fetch a NotificationTemplate by id or raise 404."""
    result = await session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)  # type: ignore[arg-type]
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@router.post("", status_code=201, response_model=NotificationTemplateResponse)
async def create_template(
    data: NotificationTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplate:
    """Create a notification template at the requested visibility scope."""
    tpl = NotificationTemplate(
        title=data.title,
        channel_hint=data.channel_hint,
        events=data.events,
        visibility=data.visibility,
        domain_name=data.domain_name,
        watched_item_id=ULID.from_str(data.watched_item_id) if data.watched_item_id else None,
        content_config=data.content_config.model_dump() if data.content_config else None,
        remote_channel_id=data.remote_channel_id,
    )
    session.add(tpl)
    await session.flush()
    audit(session, EventType.NOTIFICATION_TEMPLATE_CREATED, template_id=str(tpl.id))
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.get("", response_model=list[NotificationTemplateResponse])
async def list_templates(
    visibility: str | None = None,
    domain_name: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[NotificationTemplate]:
    """List notification templates, optionally filtered by visibility/domain."""
    stmt = select(NotificationTemplate)
    if visibility is not None:
        stmt = stmt.where(NotificationTemplate.visibility == visibility)
    if domain_name is not None:
        stmt = stmt.where(NotificationTemplate.domain_name == domain_name)
    result = await session.execute(stmt.order_by(NotificationTemplate.title))
    return list(result.scalars().all())


@router.get("/{template_id}", response_model=NotificationTemplateResponse)
async def get_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplate:
    """Fetch a single notification template by id."""
    return await _get_template_or_404(template_id, session)


@router.patch("/{template_id}", response_model=NotificationTemplateResponse)
async def update_template(
    template_id: str,
    data: NotificationTemplateUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> NotificationTemplate:
    """Partially update a notification template (visibility/refs are immutable)."""
    tpl = await _get_template_or_404(template_id, session)
    if "remote_channel_id" in data.model_fields_set and data.remote_channel_id is not None:
        tpl.remote_channel_id = data.remote_channel_id
    if "channel_hint" in data.model_fields_set and data.channel_hint is not None:
        tpl.channel_hint = data.channel_hint
    if data.events is not None:
        tpl.events = data.events
    if data.is_active is not None:
        tpl.is_active = data.is_active
    if "title" in data.model_fields_set and data.title is not None:
        tpl.title = data.title
    if "content_config" in data.model_fields_set:
        tpl.content_config = data.content_config.model_dump() if data.content_config else None
    audit(session, EventType.NOTIFICATION_TEMPLATE_UPDATED, template_id=str(tpl.id))
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a template. Templates are standalone post-#200 — no ref check needed."""
    tpl = await _get_template_or_404(template_id, session)
    audit(session, EventType.NOTIFICATION_TEMPLATE_DELETED, template_id=template_id)
    await session.delete(tpl)
    await session.commit()


@router.post("/{template_id}/test")
async def test_template(
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Send a test notification using this template's configured remote channel."""
    tpl = await _get_template_or_404(template_id, session)
    if not tpl.remote_channel_id:
        return {"success": False, "reason": "no remote_channel_id configured"}
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watched_item_id=_TEST_SENTINEL_WATCHED_ITEM_ID,
        item_name="[Test]",
        item_url="https://example.com",
        occurred_at=datetime.now(UTC),
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
                rendered_title=f"[Test] {tpl.title}",
                rendered_body=f"Test from template '{tpl.title}'.",
            )
        audit(session, EventType.NOTIFICATION_TEMPLATE_TESTED, template_id=template_id)
        await session.commit()
        return {"success": outcome.success, "reason": outcome.reason}
    except NotifierError as exc:
        return {"success": False, "reason": f"notifier error: {exc}"}
    except Exception as exc:
        return {"success": False, "reason": str(exc)}
