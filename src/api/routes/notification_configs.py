"""Notification config CRUD API endpoints (Apprise v2)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.notification_config import (
    WatchNotificationConfigCreate,
    WatchNotificationConfigResponse,
    WatchNotificationConfigUpdate,
    extract_channel_hint,
)
from src.core.crypto import encrypt_apprise_url
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.notifications.apprise_builder import get_service_name
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType

logger = get_logger(__name__)

router = APIRouter(prefix="/watches/{watch_id}/notifications", tags=["notification-configs"])


@router.post("", status_code=201, response_model=WatchNotificationConfigResponse)
async def create_notification_config(
    watch_id: str,
    data: WatchNotificationConfigCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification config for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    hint = (
        get_service_name(data.plugin_schema)
        if data.plugin_schema
        else extract_channel_hint(data.apprise_url)
    )
    config = WatchNotificationConfig(
        watch_id=watch.id,
        title=data.title,
        apprise_url=encrypt_apprise_url(data.apprise_url),
        channel_hint=hint,
        events=data.events,
        content_config=data.content_config.model_dump() if data.content_config else None,
    )
    session.add(config)
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_CREATED,
        watch_id=watch.id,
        config_id=str(config.id),
        channel_hint=config.channel_hint,
    )
    await session.commit()
    await session.refresh(config)
    return config


@router.get("", response_model=list[WatchNotificationConfigResponse])
async def list_notification_configs(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List notification configs for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    stmt = (
        select(WatchNotificationConfig)
        .where(WatchNotificationConfig.watch_id == watch.id)
        .order_by(WatchNotificationConfig.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.patch("/{config_id}", response_model=WatchNotificationConfigResponse)
async def update_notification_config(
    watch_id: str,
    config_id: str,
    data: WatchNotificationConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update is_active, events, or apprise_url on a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    if data.is_active is not None:
        nc.is_active = data.is_active
    if data.events is not None:
        nc.events = data.events
    if data.apprise_url is not None:
        nc.apprise_url = encrypt_apprise_url(data.apprise_url)
        nc.channel_hint = extract_channel_hint(data.apprise_url)
    if "title" in data.model_fields_set:
        nc.title = data.title
    if "content_config" in data.model_fields_set:
        nc.content_config = data.content_config.model_dump() if data.content_config else None
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_UPDATED,
        watch_id=watch.id,
        config_id=str(nc.id),
    )
    await session.commit()
    await session.refresh(nc)
    return nc


@router.delete("/{config_id}", status_code=204)
async def delete_notification_config(
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_DELETED,
        watch_id=watch.id,
        config_id=str(nc.id),
    )
    await session.delete(nc)
    await session.commit()


@router.post("/{config_id}/test")
async def test_notification_config(
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Send a test notification for a config. Returns {success, reason}, never 5xx."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        metadata={"test": True},
    )
    try:
        outcome = await dispatch_event(event, nc.apprise_url)
    except Exception:
        logger.exception("test notification error", extra={"config_id": config_id})
        reason = "Internal error during dispatch"
        success = False
    else:
        success = outcome.success
        reason = outcome.reason
    audit(
        session,
        EventType.NOTIFICATION_TEST,
        watch_id=watch.id,
        config_id=str(nc.id),
        channel_hint=nc.channel_hint,
        success=success,
        reason=reason,
    )
    await session.commit()
    return {"success": success, "reason": reason}
