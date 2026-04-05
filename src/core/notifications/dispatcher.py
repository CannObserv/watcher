"""Apprise-based notification dispatcher."""

from dataclasses import dataclass

import apprise

from src.core.crypto import decrypt_apprise_url
from src.core.logging import get_logger
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of a single Apprise dispatch attempt."""

    success: bool
    reason: str


async def dispatch_event(event: WatchEvent, apprise_url_encrypted: str) -> DispatchResult:
    """Dispatch a WatchEvent to a single Apprise target.

    Decrypts the stored URL, hands it to Apprise, and awaits async_notify.
    Returns a DispatchResult with success flag and human-readable reason.
    """
    url = decrypt_apprise_url(apprise_url_encrypted)
    ap = apprise.Apprise()
    if not ap.add(url):
        logger.warning(
            "invalid apprise url in notification config",
            extra={"watch_id": event.watch_id, "event_type": event.event_type},
        )
        return DispatchResult(
            success=False, reason="Invalid Apprise URL — check your configuration"
        )
    result = await ap.async_notify(
        body=event.body,
        title=event.title,
        notify_type=event.apprise_notify_type,
    )
    if result is True:
        return DispatchResult(success=True, reason="Notification sent successfully")
    return DispatchResult(
        success=False,
        reason="Delivery failed — the service rejected or could not process the request",
    )
