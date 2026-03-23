"""Tests for SlackChannel."""

import json

import httpx
import pytest

from src.core.notifications.slack import SlackChannel


class TestSlackChannel:
    """SlackChannel posts to Slack incoming webhooks."""

    @pytest.fixture
    def captured(self):
        return {}

    def _make_channel(self, status_code: int, captured: dict | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            if captured is not None:
                captured["url"] = str(request.url)
                captured["body"] = json.loads(request.content)
            return httpx.Response(status_code)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        return SlackChannel(client)

    async def test_sends_to_webhook_url(self, captured, make_event):
        channel = self._make_channel(200, captured)
        result = await channel.send(
            make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"}
        )
        assert result is True
        assert captured["url"] == "https://hooks.slack.com/T1/B1/xxx"

    async def test_payload_has_text(self, captured, make_event):
        channel = self._make_channel(200, captured)
        await channel.send(make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"})
        assert "Test Watch" in captured["body"]["text"]

    async def test_returns_false_on_error(self, make_event):
        channel = self._make_channel(500)
        result = await channel.send(
            make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"}
        )
        assert result is False
