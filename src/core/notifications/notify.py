"""Notification delivery through the notifier service — test sends only.

After Phase 5 (#137), the local Apprise dispatcher is gone — the notifier
service is the only delivery path. Every candidate must carry a
`remote_channel_id`; missing values are recorded as failures.

**Watcher dispatches no event of its own (#376, F3).** Archiver has sent every
notification for an event at or after 2026-11-06T21:00:00Z, from the
``info.watch-events`` facts Watcher stages (#369); part 2 deleted the
dispatcher and its three sites. What is left serves the operator's test-send
routes, which #379 deletes with the rest of the subsystem.
"""

from dataclasses import dataclass

from notifier_client import NotifierClient
from notifier_client.errors import NotifierError
from notifier_client.types import DispatchOutStatus

from src.core.logging import get_logger
from src.core.notifications.events import WatchEvent
from src.core.notifier_client import build_idempotency_key

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
