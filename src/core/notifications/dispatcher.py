"""Apprise-based notification dispatcher."""

import contextvars
import logging
from dataclasses import dataclass

import apprise
from apprise import AppriseAsset

from src.core.crypto import decrypt_apprise_url
from src.core.logging import get_logger
from src.core.notifications.events import WatchEvent

logger = get_logger(__name__)

# Watcher brand identity for all outbound notifications.
# image_url_mask/logo suppressed (empty) so plugins don't pull Apprise CDN icons.
_ASSET = AppriseAsset(
    app_id="CO Watcher",
    app_desc="Cannabis Observer Watcher",
    app_url="https://watcher.exe.xyz",
    image_url_mask="",
    image_url_logo="",
)

# Per-task capture buffer for Apprise WARNING log messages.
# Each asyncio task gets its own context copy, so concurrent dispatch_event
# calls are fully isolated — no cross-contamination between watches.
_capture_ctx: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_apprise_capture", default=None
)


class _AppriseCapturingFilter(logging.Filter):
    """Appends WARNING+ apprise log messages to the current task's capture buffer."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            buf = _capture_ctx.get()
            if buf is not None:
                buf.append(record.getMessage())
        return True  # never suppress


# Attached once at module load; zero cost when no capture buffer is set.
logging.getLogger("apprise").addFilter(_AppriseCapturingFilter())


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of a single Apprise dispatch attempt."""

    success: bool
    reason: str


async def dispatch_event(
    event: WatchEvent,
    apprise_url_encrypted: str,
    *,
    body: str | None = None,
) -> DispatchResult:
    """Dispatch a WatchEvent to a single Apprise target.

    Decrypts the stored URL, hands it to Apprise, and awaits async_notify.
    Returns a DispatchResult with success flag and human-readable reason.
    Apprise WARNING log messages emitted during the call are captured and
    included in the reason on failure, surfacing actionable error detail
    (e.g. Slack's not_in_channel, HTTP 401 bodies).

    body — if provided, overrides event.body for this dispatch. Use this to
    send per-config customised content while preserving the event title and
    notify_type.
    """
    url = decrypt_apprise_url(apprise_url_encrypted)
    ap = apprise.Apprise(asset=_ASSET)
    if not ap.add(url):
        logger.warning(
            "invalid apprise url in notification config",
            extra={"watch_id": event.watch_id, "event_type": event.event_type},
        )
        return DispatchResult(success=False, reason="Invalid Apprise URL: check your configuration")

    messages: list[str] = []
    token = _capture_ctx.set(messages)
    try:
        result = await ap.async_notify(
            body=body if body is not None else event.body,
            title=event.title,
            notify_type=event.apprise_notify_type,
        )
    finally:
        _capture_ctx.reset(token)

    if result is True:
        return DispatchResult(success=True, reason="Notification sent successfully")
    detail = "; ".join(messages) or "no detail captured"
    return DispatchResult(success=False, reason=f"Delivery failed: {detail}")
