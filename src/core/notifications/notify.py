"""Notification dispatch for watch lifecycle events."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.watch import Watch
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


@dataclass
class DispatchCandidate:
    """A single notification target, drawn from global, domain, watch, or local source."""

    apprise_url: str
    source: str  # "global" | "domain" | "watch_template" | "local"
    source_id: str


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in notification targets.

    Queries four live sources in priority order:
      1. Global templates (NotificationTemplate.is_global_default=True) — all watches
      2. Domain templates (DomainNcRef) — watches whose effective_domain matches
      3. Watch-assigned templates (WatchNcRef) — this watch only, deduped vs. 1+2
      4. Local configs (WatchNotificationConfig) — this watch only

    Template sources are deduplicated by template_id so a template that appears in
    multiple sources (e.g. global AND manually assigned via WatchNcRef) fires once.
    Failures are logged but never raise. Writes a single audit log entry. Does not
    commit; caller is responsible.
    """
    watch_ulid = ULID.from_str(event.watch_id)
    event_value = event.event_type.value

    # Resolve effective_domain for this watch
    domain_row = await session.execute(select(Watch.effective_domain).where(Watch.id == watch_ulid))
    effective_domain: str | None = domain_row.scalar_one_or_none()

    # 1. Global templates
    global_result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.is_global_default.is_(True),
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event_value]),
        )
    )
    global_templates = global_result.scalars().all()

    # 2. Domain templates
    domain_templates = []
    if effective_domain:
        domain_result = await session.execute(
            select(NotificationTemplate)
            .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
            .where(
                DomainNcRef.domain_name == effective_domain,
                NotificationTemplate.is_active.is_(True),
                NotificationTemplate.events.contains([event_value]),
            )
        )
        domain_templates = domain_result.scalars().all()

    # 3. Watch-assigned templates (WatchNcRef)
    watch_tpl_result = await session.execute(
        select(NotificationTemplate)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(
            WatchNcRef.watch_id == watch_ulid,
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event_value]),
        )
    )
    watch_templates = watch_tpl_result.scalars().all()

    # 4. Local configs
    local_result = await session.execute(
        select(WatchNotificationConfig).where(
            WatchNotificationConfig.watch_id == watch_ulid,
            WatchNotificationConfig.is_active.is_(True),
            WatchNotificationConfig.events.contains([event_value]),
        )
    )
    local_configs = local_result.scalars().all()

    # Build deduped candidate list: templates first (global → domain → watch), then local
    seen_template_ids: set[str] = set()
    candidates: list[DispatchCandidate] = []

    for source, tpl_list in [
        ("global", global_templates),
        ("domain", domain_templates),
        ("watch_template", watch_templates),
    ]:
        for tpl in tpl_list:
            tpl_id = str(tpl.id)
            if tpl_id not in seen_template_ids:
                seen_template_ids.add(tpl_id)
                candidates.append(
                    DispatchCandidate(
                        apprise_url=tpl.apprise_url,
                        source=source,
                        source_id=tpl_id,
                    )
                )

    for c in local_configs:
        candidates.append(
            DispatchCandidate(
                apprise_url=c.apprise_url,
                source="local",
                source_id=str(c.id),
            )
        )

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
