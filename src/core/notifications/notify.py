"""Notification dispatch for watch lifecycle events.

After Phase 5 (#137), the local Apprise dispatcher is gone — the notifier
service is the only delivery path. Every candidate must carry a
`remote_channel_id`; missing values are recorded as failures.
"""

from dataclasses import dataclass

from notifier_client import NotifierClient
from notifier_client.errors import NotifierError
from notifier_client.types import DispatchOutStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.watch import Watch
from src.core.notifications.content import (
    build_body,
    build_title,
    resolve_options,
)
from src.core.notifications.events import WatchEvent
from src.core.notifier_client import build_idempotency_key, get_notifier_client

logger = get_logger(__name__)


@dataclass
class DispatchResult:
    """Outcome of a single notifier dispatch attempt."""

    success: bool
    reason: str


@dataclass
class DispatchCandidate:
    """A single notification target, drawn from global, domain, watch, or local source."""

    source: str  # "global" | "domain" | "watch_template" | "local"
    source_id: str
    content_config: dict | None = None
    remote_channel_id: str | None = None


async def dispatch_via_notifier(
    client: NotifierClient,
    candidate: DispatchCandidate,
    event: WatchEvent,
    rendered_title: str,
    rendered_body: str,
) -> DispatchResult:
    """Call the notifier service to record and deliver a pre-rendered notification.

    Passes the already-rendered title/body as inline templates with empty variables
    so notifier stores the final text and handles delivery + attempt logging.
    Returns a DispatchResult mirroring the notifier attempt outcome.

    `client` is owned by the caller; this function neither opens nor closes it.
    """
    idem_key = build_idempotency_key(event, candidate.source_id)
    try:
        out = await client.dispatch(
            title_template=rendered_title,
            body_template=rendered_body,
            variables={},
            channel_ids=[candidate.remote_channel_id],
            idempotency_key=idem_key,
            metadata={
                "event_type": event.event_type.value,
                "watch_id": event.watch_id,
                "source": candidate.source,
                "source_id": candidate.source_id,
            },
        )
        if out.status == DispatchOutStatus.SUCCEEDED:
            return DispatchResult(success=True, reason=f"notifier:{out.id}")
        # PARTIAL is unreachable here: watcher passes a single channel_id per
        # candidate, so notifier either fully succeeds or fully fails.
        reason = "Delivery failed via notifier"
        if out.attempts:
            reason = out.attempts[0].reason or reason
        return DispatchResult(success=False, reason=reason)
    except NotifierError as exc:
        logger.warning(
            "notifier API error during dispatch",
            extra={
                "source": candidate.source,
                "source_id": candidate.source_id,
                "watch_id": event.watch_id,
            },
            exc_info=True,
        )
        return DispatchResult(success=False, reason=f"notifier error: {exc}")


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

    Every candidate is dispatched via the notifier service. Candidates that lack
    a `remote_channel_id` are recorded as failed audit results (the local Apprise
    fallback was removed in #137).

    After Phase 5, the Snapshot/Change tables are gone — unified diff is no longer
    computed. Templates receive `unified_diff=""` and diff_snippet/diff_full render
    empty (page-level fingerprint-shift-only notifications).
    """
    watch_ulid = ULID.from_str(event.watch_id)
    event_value = event.event_type.value

    # Resolve effective_domain for this watch in one query.
    watch_row = await session.execute(
        select(Watch.effective_domain, Watch.content_type).where(Watch.id == watch_ulid)
    )
    watch_meta = watch_row.one_or_none()
    effective_domain: str | None
    if watch_meta is None:
        effective_domain = None
    else:
        effective_domain, _ = watch_meta

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
                        source=source,
                        source_id=tpl_id,
                        content_config=tpl.content_config,
                        remote_channel_id=tpl.remote_channel_id,
                    )
                )

    for c in local_configs:
        candidates.append(
            DispatchCandidate(
                source="local",
                source_id=str(c.id),
                content_config=c.content_config,
                remote_channel_id=c.remote_channel_id,
            )
        )

    if not candidates:
        return

    # Snapshot/Change tables removed in Phase 5 — diff is always empty.
    # Templates still receive `unified_diff` so diff_snippet/diff_full render
    # as empty strings rather than causing template errors.
    unified_diff: str = ""

    results = []
    async with get_notifier_client() as notifier_client:
        for candidate in candidates:
            try:
                cfg = (
                    ContentConfig.model_validate(candidate.content_config)
                    if candidate.content_config
                    else None
                )
                options = resolve_options(cfg, event_value)
                rendered_title = build_title(event, options)
                rendered_body = build_body(event, options, unified_diff=unified_diff)
                if not candidate.remote_channel_id:
                    logger.warning(
                        "candidate has no remote_channel_id; skipping dispatch",
                        extra={"source": candidate.source, "source_id": candidate.source_id},
                    )
                    result = DispatchResult(
                        success=False,
                        reason="no remote_channel_id configured",
                    )
                else:
                    result = await dispatch_via_notifier(
                        notifier_client, candidate, event, rendered_title, rendered_body
                    )
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
