"""Notification config CRUD API endpoints (remote-channel only).

After Phase 5 (#137), notification configs are pure remote-channel pointers:
no Apprise URL is stored or validated here. The notifier service owns the
actual delivery target.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from notifier_client.errors import NotifierError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import get_watch_or_404, parse_ulid
from src.api.schemas.notification_config import (
    WatchNotificationConfigCreate,
    WatchNotificationConfigResponse,
    WatchNotificationConfigUpdate,
)
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import DispatchCandidate, dispatch_via_notifier
from src.core.notifier_client import get_notifier_client
from src.core.registry import get_registry
from src.core.watches import resolve_watch_url

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
    config = WatchNotificationConfig(
        watch_id=watch.id,
        title=data.title,
        channel_hint=data.channel_hint,
        events=data.events,
        content_config=data.content_config.model_dump() if data.content_config else None,
        remote_channel_id=data.remote_channel_id,
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
    """Update is_active, events, channel_hint, or remote_channel_id on a notification config."""
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    if data.is_active is not None:
        nc.is_active = data.is_active
    if data.events is not None:
        nc.events = data.events
    if "channel_hint" in data.model_fields_set and data.channel_hint is not None:
        nc.channel_hint = data.channel_hint
    if "remote_channel_id" in data.model_fields_set:
        nc.remote_channel_id = data.remote_channel_id
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
    """Send a test notification for a config via the notifier service.

    Returns {success, reason}, never 5xx.
    """
    watch = await get_watch_or_404(watch_id, session)
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    success = False
    reason = "Internal error during dispatch"
    try:
        info_client = get_registry().get_archiver_client()
        try:
            resolved_url = await resolve_watch_url(watch, info_client)
        except Exception as exc:
            logger.exception(
                "failed to resolve watch URL for test notification",
                extra={"config_id": config_id, "watch_id": str(watch.id)},
            )
            reason = f"Failed to resolve watch URL: {exc}"
        else:
            if not nc.remote_channel_id:
                reason = "no remote_channel_id configured"
            else:
                event = WatchEvent(
                    event_type=WatchEventType.CHANGE_DETECTED,
                    watch_id=str(watch.id),
                    watch_name=watch.name,
                    watch_url=resolved_url,
                    occurred_at=datetime.now(UTC),
                    metadata={"test": True},
                )
                candidate = DispatchCandidate(
                    source="local",
                    source_id=str(nc.id),
                    content_config=nc.content_config,
                    remote_channel_id=nc.remote_channel_id,
                )
                try:
                    async with get_notifier_client() as client:
                        outcome = await dispatch_via_notifier(
                            client,
                            candidate,
                            event,
                            rendered_title="[Test] Watch notification",
                            rendered_body=f"Test from watch '{watch.name}'.",
                        )
                    success = outcome.success
                    reason = outcome.reason
                except NotifierError as exc:
                    reason = f"notifier error: {exc}"
    except Exception:
        logger.exception("test notification error", extra={"config_id": config_id})
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
