"""Notification dispatcher — route events to configured channels."""

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


async def dispatch_notifications(
    event: ChangeEvent,
    configs: list[dict],
    channels: dict,
) -> list[dict]:
    """Dispatch a change event to all configured notification channels.

    Args:
        event: The change event to notify about.
        configs: List of notification config dicts, each with a "channel" key.
        channels: Map of channel name -> channel instance.

    Returns:
        List of result dicts: {"channel", "success", "error"?}
    """
    results = []
    for config in configs:
        channel_name = config.get("channel", "unknown")
        channel = channels.get(channel_name)
        if not channel:
            logger.warning(
                "unknown notification channel",
                extra={"channel": channel_name},
            )
            results.append({
                "channel": channel_name,
                "success": False,
                "error": f"unknown channel: {channel_name}",
            })
            continue
        try:
            success = await channel.send(event, config)
            results.append({"channel": channel_name, "success": success})
            extra = {"channel": channel_name, "watch_id": event.watch_id}
            if success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception("notification error", extra={"channel": channel_name})
            results.append({"channel": channel_name, "success": False, "error": "exception"})
    return results
