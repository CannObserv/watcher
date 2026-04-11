"""Notification dispatch for watch lifecycle events."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import NotificationTemplate, WatchNcRef
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


@dataclass
class DispatchCandidate:
    """A single notification target, drawn from either a local config or a template ref."""

    apprise_url: str
    source: str  # "local" | "template"
    source_id: str


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in notification configs and template refs.

    Queries local WatchNotificationConfig rows and NotificationTemplate rows joined via
    WatchNcRef, unioning both into DispatchCandidate instances. Dispatches sequentially.
    Failures are logged but never raise. Writes a single audit log entry with per-candidate
    results. Does not commit; caller is responsible.
    """
    watch_ulid = ULID.from_str(event.watch_id)

    # 1. Local watch_notification_configs
    local_result = await session.execute(
        select(WatchNotificationConfig).where(
            WatchNotificationConfig.watch_id == watch_ulid,
            WatchNotificationConfig.is_active.is_(True),
            WatchNotificationConfig.events.contains([event.event_type.value]),
        )
    )
    local_configs = local_result.scalars().all()

    # 2. Template refs via watch_nc_refs
    template_result = await session.execute(
        select(NotificationTemplate)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(
            WatchNcRef.watch_id == watch_ulid,
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event.event_type.value]),
        )
    )
    templates = template_result.scalars().all()

    candidates: list[DispatchCandidate] = [
        DispatchCandidate(apprise_url=c.apprise_url, source="local", source_id=str(c.id))
        for c in local_configs
    ] + [
        DispatchCandidate(apprise_url=t.apprise_url, source="template", source_id=str(t.id))
        for t in templates
    ]

    if not candidates:
        return

    results = []
    for candidate in candidates:
        try:
            result = await dispatch_event(event, candidate.apprise_url)
            results.append(
                {
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "success": result.success,
                    "reason": result.reason,
                }
            )
            extra = {
                "source": candidate.source,
                "source_id": candidate.source_id,
                "watch_id": event.watch_id,
                "event_type": event.event_type,
            }
            if result.success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception(
                "notification dispatch error",
                extra={"source": candidate.source, "source_id": candidate.source_id},
            )
            results.append(
                {
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "success": False,
                    "reason": "exception",
                }
            )

    audit(
        session,
        EventType.NOTIFICATION_DISPATCHED,
        watch_id=event.watch_id,
        watch_event_type=event.event_type,
        results=results,
    )
