"""Slack notification channel — POST to an incoming webhook."""

import httpx
from pydantic import BaseModel, HttpUrl

from src.core.logging import get_logger
from src.core.notifications.base import ChangeEvent

logger = get_logger(__name__)


class SlackConfig(BaseModel):
    """Pydantic config model for SlackChannel."""

    webhook_url: HttpUrl


class SlackChannel:
    """Deliver change notifications to a Slack incoming webhook."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

    async def send(self, event: ChangeEvent, config: dict) -> bool:
        """POST a Slack message to *config['webhook_url']*. Return True on success."""
        cfg = SlackConfig.model_validate(config)
        url = str(cfg.webhook_url)

        payload = {
            "text": f"Change detected: {event.watch_name}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{event.watch_url}|{event.watch_name}>*\n{event.summary}",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"Change ID: `{event.change_id}`"
                                f" | Detected: {event.detected_at.isoformat()}"
                            ),
                        }
                    ],
                },
            ],
        }

        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "slack_http_error",
                extra={"status": exc.response.status_code, "url": url},
            )
            return False
        except httpx.ConnectError:
            logger.warning("slack_connect_error", extra={"url": url})
            return False
