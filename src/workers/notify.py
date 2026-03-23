"""Notification dispatch for detected watch changes."""

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_logger
from src.core.models.audit_log import AuditLog
from src.core.models.notification_config import NotificationConfig
from src.core.models.watch import Watch
from src.core.notifications import ChangeEvent, EmailChannel, SlackChannel, WebhookChannel
from src.core.notifications.dispatcher import dispatch_notifications

logger = get_logger(__name__)


async def dispatch_change_notifications(
    session: AsyncSession,
    watch: Watch,
    change_id: str,
    change_metadata: dict,
) -> None:
    """Dispatch notifications for a detected change and write an audit log entry.

    Fetches active NotificationConfig records for the watch, builds a ChangeEvent,
    and calls dispatch_notifications with the configured channels. Does not commit
    the session; caller is responsible for committing.
    """
    nc_stmt = select(NotificationConfig).where(
        NotificationConfig.watch_id == watch.id,
        NotificationConfig.is_active.is_(True),
    )
    nc_result = await session.execute(nc_stmt)
    nc_configs = [{"channel": nc.channel, **nc.config} for nc in nc_result.scalars().all()]
    if not nc_configs:
        return

    event = ChangeEvent(
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        change_id=change_id,
        detected_at=datetime.now(UTC),
        change_metadata=change_metadata,
    )
    async with httpx.AsyncClient() as http_client:
        channels = {
            "webhook": WebhookChannel(client=http_client),
            "email": EmailChannel(),
            "slack": SlackChannel(client=http_client),
        }
        notif_results = await dispatch_notifications(event, nc_configs, channels)

    session.add(
        AuditLog(
            event_type="notification.dispatched",
            watch_id=watch.id,
            payload={
                "change_id": change_id,
                "results": notif_results,
            },
        )
    )
