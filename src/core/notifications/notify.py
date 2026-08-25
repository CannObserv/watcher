"""Notification dispatch for watch lifecycle events.

After Phase 5 (#137), the local Apprise dispatcher is gone — the notifier
service is the only delivery path. Every candidate must carry a
`remote_channel_id`; missing values are recorded as failures.
"""

from dataclasses import dataclass

from notifier_client import NotifierClient
from notifier_client.errors import NotifierError
from notifier_client.types import DispatchOutStatus
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.watched_item import WatchedItem
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
    """A single notification target — one NotificationTemplate row.

    ``source`` is the template's ``visibility``: "global" | "domain" |
    "watched_item".
    """

    source: str
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
                "watched_item_id": event.watched_item_id,
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
                "watched_item_id": event.watched_item_id,
            },
            exc_info=True,
        )
        return DispatchResult(success=False, reason=f"notifier error: {exc}")


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in notification templates.

    Post-#200 every notification target is a single ``NotificationTemplate`` row
    with an intrinsic ``visibility``. One query selects the active templates whose
    ``events`` include this event and whose visibility matches the WatchedItem:

      * ``global`` — every WatchedItem
      * ``domain`` — WatchedItems whose ``domain_name`` matches
      * ``watched_item`` — this WatchedItem only

    **Dedup rule:** one notification fires per matching template row. Because a
    single query returns each row once, id-dedup is automatic — no row appears
    twice. Multiple templates may target the same ``remote_channel_id`` and all
    fire; there is no channel-level suppression (ratified in #200, F2).

    Failures are logged but never raise. Writes a single audit log entry. Does not
    commit; caller is responsible.

    Every candidate is dispatched via the notifier service. Candidates that lack
    a `remote_channel_id` are recorded as failed audit results (the local Apprise
    fallback was removed in #137).
    """
    # #191: the event identifies a WatchedItem (the single monitored entity).
    watched_item_id = ULID.from_str(event.watched_item_id)
    event_value = event.event_type.value

    wi = await session.get(WatchedItem, watched_item_id)
    domain_name: str | None = wi.domain_name if wi is not None else None

    visibility_clauses = [
        NotificationTemplate.visibility == VISIBILITY_GLOBAL,
        and_(
            NotificationTemplate.visibility == VISIBILITY_WATCHED_ITEM,
            NotificationTemplate.watched_item_id == watched_item_id,
        ),
    ]
    if domain_name:
        visibility_clauses.append(
            and_(
                NotificationTemplate.visibility == VISIBILITY_DOMAIN,
                NotificationTemplate.domain_name == domain_name,
            )
        )

    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event_value]),
            or_(*visibility_clauses),
        )
    )
    templates = result.scalars().all()

    candidates: list[DispatchCandidate] = [
        DispatchCandidate(
            source=tpl.visibility,
            source_id=str(tpl.id),
            content_config=tpl.content_config,
            remote_channel_id=tpl.remote_channel_id,
        )
        for tpl in templates
    ]

    if not candidates:
        return

    results = []
    # Outside the per-candidate try below, deliberately (CR-6, #277): a client
    # that cannot be built is a process-level misconfiguration, not a failed
    # dispatch, so it must not be recorded as one per candidate. The two
    # failure modes it raises — the "unset" RuntimeErrors and
    # NotifierNotEnabled — are both unreachable in a correctly launched
    # process: the lifespan gate refuses to start on a URL held without the
    # opt-in, and refuses nothing when no URL is configured at all. If one ever
    # does escape here it surfaces as a task failure, which is the honest
    # signal; the two route call sites catch broad Exception for the same
    # reason, since a 500 there would be less useful than a reason string.
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
                rendered_body = build_body(event, options)
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
                    "watched_item_id": event.watched_item_id,
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
        watched_item_id=event.watched_item_id,
        watch_event_type=event.event_type,
        results=results,
    )
