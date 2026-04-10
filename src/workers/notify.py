"""Notification dispatch for watch lifecycle events."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in WatchNotificationConfig rows.

    Queries configs where watch_id matches, is_active is True, and the event
    type code is in the events array. Dispatches sequentially.
    Failures are logged but never raise. Writes a single audit log entry
    with per-config results. Does not commit; caller is responsible.
    """
    stmt = select(WatchNotificationConfig).where(
        WatchNotificationConfig.watch_id == ULID.from_str(event.watch_id),
        WatchNotificationConfig.is_active.is_(True),
        WatchNotificationConfig.events.contains([event.event_type.value]),
    )
    result = await session.execute(stmt)
    configs = result.scalars().all()
    if not configs:
        return

    results = []
    for config in configs:
        try:
            outcome = await dispatch_event(event, config.apprise_url)
            results.append(
                {
                    "config_id": str(config.id),
                    "success": outcome.success,
                    "reason": outcome.reason,
                }
            )
            extra = {
                "config_id": str(config.id),
                "watch_id": event.watch_id,
                "event_type": event.event_type,
            }
            if outcome.success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception("notification error", extra={"config_id": str(config.id)})
            results.append({"config_id": str(config.id), "success": False, "reason": "exception"})

    audit(
        session,
        EventType.NOTIFICATION_DISPATCHED,
        watch_id=event.watch_id,
        watch_event_type=event.event_type,
        results=results,
    )
