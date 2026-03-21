"""Tests for WebhookChannel."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.webhook import WebhookChannel


def _make_event(**overrides):
    defaults = {
        "watch_id": "w1",
        "watch_name": "Test Watch",
        "watch_url": "https://example.com",
        "change_id": "c1",
        "detected_at": datetime(2026, 1, 1, tzinfo=UTC),
        "change_metadata": {"added": 2, "modified": 1, "removed": 0},
    }
    defaults.update(overrides)
    return ChangeEvent(**defaults)


class TestWebhookChannel:
    """WebhookChannel sends JSON POST requests."""

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
        return WebhookChannel(client)

    async def test_sends_post_with_json_payload(self, captured):
        channel = self._make_channel(200, captured)
        result = await channel.send(_make_event(), {"url": "https://hook.example.com/abc"})
        assert result is True
        assert captured["url"] == "https://hook.example.com/abc"
        assert isinstance(captured["body"], dict)

    async def test_includes_event_data_in_payload(self, captured):
        channel = self._make_channel(200, captured)
        event = _make_event(watch_name="My Watch", change_id="c42")
        await channel.send(event, {"url": "https://hook.example.com/abc"})
        assert captured["body"]["watch_name"] == "My Watch"
        assert captured["body"]["change_id"] == "c42"

    async def test_returns_false_on_http_error(self):
        channel = self._make_channel(500)
        result = await channel.send(_make_event(), {"url": "https://hook.example.com/abc"})
        assert result is False

    async def test_returns_false_on_connection_error(self):
        def raise_connect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(raise_connect)
        client = httpx.AsyncClient(transport=transport)
        channel = WebhookChannel(client)
        result = await channel.send(_make_event(), {"url": "https://hook.example.com/abc"})
        assert result is False
