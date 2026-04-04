"""Apprise-based notification dispatcher."""

import apprise

from src.core.crypto import decrypt_apprise_url
from src.core.logging import get_logger
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)


async def dispatch_event(event: WatchEvent, apprise_url_encrypted: str) -> bool:
    """Dispatch a WatchEvent to a single Apprise target.

    Decrypts the stored URL, hands it to Apprise, and awaits async_notify.
    Returns True on success, False on failure or if nothing was dispatched.
    """
    url = decrypt_apprise_url(apprise_url_encrypted)
    ap = apprise.Apprise()
    if not ap.add(url):
        logger.warning(
            "invalid apprise url in notification config",
            extra={"watch_id": event.watch_id, "event_type": event.event_type},
        )
        return False
    result = await ap.async_notify(
        body=event.body,
        title=event.title,
        notify_type=event.apprise_notify_type,
    )
    return result is True
