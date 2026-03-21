"""Webhook notification channel — POST JSON to a URL."""

import httpx

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class WebhookChannel:
    """Deliver change notifications via HTTP POST with a JSON payload."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """POST event data to *config['url']*. Return True on 2xx."""
        url = config.get("url")
        if not url:
            logger.warning("webhook_missing_url")
            return False

        payload = {
            "watch_id": event.watch_id,
            "watch_name": event.watch_name,
            "watch_url": event.watch_url,
            "change_id": event.change_id,
            "detected_at": event.detected_at.isoformat(),
            "summary": event.summary,
            "change_metadata": event.change_metadata,
        }

        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning("webhook_http_error", extra={"status": exc.response.status_code, "url": url})
            return False
        except httpx.ConnectError:
            logger.warning("webhook_connect_error", extra={"url": url})
            return False
