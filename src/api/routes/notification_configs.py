"""Notification config CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigResponse,
)
from src.core.models.audit_log import AuditLog
from src.core.models.notification_config import NotificationConfig

router = APIRouter(
    prefix="/api/watches/{watch_id}/notifications", tags=["notification-configs"]
)


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
        channel=data.channel,
        config=data.config,
    )
    session.add(config)
    audit = AuditLog(
        event_type="notification_config.created",
        watch_id=watch.id,
        payload={"config_id": str(config.id), "channel": data.channel},
    )
    session.add(audit)
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
    audit = AuditLog(
        event_type="notification_config.deleted",
        watch_id=watch.id,
        payload={"config_id": str(nc.id)},
    )
    session.add(audit)
    await session.delete(nc)
    await session.commit()
