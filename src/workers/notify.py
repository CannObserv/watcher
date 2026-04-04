"""Notification dispatch for watch lifecycle events."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import NotificationConfig
from src.core.models.watch import Watch
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.registry import ServiceRegistry

logger = get_logger(__name__)


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in NotificationConfig rows.

    Queries configs where watch_id matches, is_active is True, and the event
    type code is in the events array. Dispatches sequentially.
    Failures are logged but never raise. Writes a single audit log entry
    with per-config results. Does not commit; caller is responsible.
    """
    stmt = select(NotificationConfig).where(
        NotificationConfig.watch_id == ULID.from_str(event.watch_id),
        NotificationConfig.is_active.is_(True),
        NotificationConfig.events.contains([event.event_type.value]),
    )
    result = await session.execute(stmt)
    configs = result.scalars().all()
    if not configs:
        return

    results = []
    for config in configs:
        try:
            success = await dispatch_event(event, config.apprise_url)
            results.append({"config_id": str(config.id), "success": success})
            extra = {
                "config_id": str(config.id),
                "watch_id": event.watch_id,
                "event_type": event.event_type,
            }
            if success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception("notification error", extra={"config_id": str(config.id)})
            results.append({"config_id": str(config.id), "success": False, "error": "exception"})

    audit(
        session,
        EventType.NOTIFICATION_DISPATCHED,
        watch_id=event.watch_id,
        watch_event_type=event.event_type,
        results=results,
    )


async def dispatch_change_notifications(
    session: AsyncSession,
    watch: Watch,
    change_id: str,
    change_metadata: dict,
    registry: ServiceRegistry | None = None,
) -> None:
    """Dispatch notifications for a detected change.

    Deprecated: Use dispatch_event_notifications with WatchEvent directly.
    This wrapper is maintained for backward compatibility until Task 11 cleanup.

    Builds a CHANGE_DETECTED WatchEvent and dispatches to all active,
    opted-in configs for the watch. Does not commit the session.
    """
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        metadata=change_metadata,
    )
    await dispatch_event_notifications(session, event)
