"""Notification config CRUD API endpoints (Apprise v2)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigResponse,
    NotificationConfigUpdate,
    extract_channel_hint,
)
from src.core.crypto import encrypt_apprise_url
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import NotificationConfig

router = APIRouter(prefix="/watches/{watch_id}/notifications", tags=["notification-configs"])


@router.post("", status_code=201, response_model=NotificationConfigResponse)
async def create_notification_config(
    watch_id: str,
    data: NotificationConfigCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification config for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    config = NotificationConfig(
        watch_id=watch.id,
        apprise_url=encrypt_apprise_url(data.apprise_url),
        channel_hint=extract_channel_hint(data.apprise_url),
        events=data.events,
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


@router.get("", response_model=list[NotificationConfigResponse])
async def list_notification_configs(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List notification configs for a watch."""
    watch = await get_watch_or_404(watch_id, session)
    stmt = (
        select(NotificationConfig)
        .where(NotificationConfig.watch_id == watch.id)
        .order_by(NotificationConfig.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.patch("/{config_id}", response_model=NotificationConfigResponse)
async def update_notification_config(
    watch_id: str,
    config_id: str,
    data: NotificationConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update is_active or events on a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    if data.is_active is not None:
        nc.is_active = data.is_active
    if data.events is not None:
        nc.events = data.events
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
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
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
