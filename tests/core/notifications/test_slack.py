"""Tests for SlackChannel."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.slack import SlackChannel


def _make_event(**overrides):
    defaults = {
        "watch_id": "w1",
        "watch_name": "Test Watch",
        "watch_url": "https://example.com",
        "change_id": "c1",
        "detected_at": datetime(2026, 1, 1, tzinfo=UTC),
        "change_metadata": {"added": ["Page 2", "Page 3"], "modified": ["Page 1"], "removed": []},
    }
    defaults.update(overrides)
    return ChangeEvent(**defaults)


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

    async def test_sends_to_webhook_url(self, captured):
        channel = self._make_channel(200, captured)
        result = await channel.send(
            _make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"}
        )
        assert result is True
        assert captured["url"] == "https://hooks.slack.com/T1/B1/xxx"

    async def test_payload_has_text(self, captured):
        channel = self._make_channel(200, captured)
        await channel.send(_make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"})
        assert "Test Watch" in captured["body"]["text"]

    async def test_returns_false_on_error(self):
        channel = self._make_channel(500)
        result = await channel.send(
            _make_event(), {"webhook_url": "https://hooks.slack.com/T1/B1/xxx"}
        )
        assert result is False
