"""Tests for notification dispatcher."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.core.notifications.base import ChangeEvent
from src.core.notifications.dispatcher import dispatch_notifications


@pytest.fixture
def event():
    return ChangeEvent(
        watch_id="01KM7A9TP2B0BQCNZ5PZX4MH89",
        watch_name="Test Watch",
        watch_url="https://example.com",
        change_id="01KM7A9TP2B0BQCNZ5PZX4MH8A",
        detected_at=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
        change_metadata={"added": ["Page 2"], "modified": [], "removed": []},
    )


class TestDispatchNotifications:
    async def test_dispatches_to_all_configs(self, event):
        configs = [
            {"channel": "webhook", "url": "https://hooks.example.com/a"},
            {"channel": "webhook", "url": "https://hooks.example.com/b"},
        ]
        mock_channel = AsyncMock()
        mock_channel.send.return_value = True
        results = await dispatch_notifications(event, configs, {"webhook": mock_channel})
        assert mock_channel.send.call_count == 2
        assert all(r["success"] for r in results)

    async def test_unknown_channel_skipped(self, event):
        results = await dispatch_notifications(event, [{"channel": "pigeon"}], {})
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "unknown" in results[0]["error"]

    async def test_channel_failure_does_not_block_others(self, event):
        configs = [
            {"channel": "webhook", "url": "https://fail.example.com"},
            {"channel": "slack", "webhook_url": "https://hooks.slack.com/ok"},
        ]
        fail_ch = AsyncMock()
        fail_ch.send.return_value = False
        ok_ch = AsyncMock()
        ok_ch.send.return_value = True
        results = await dispatch_notifications(event, configs, {"webhook": fail_ch, "slack": ok_ch})
        assert results[0]["success"] is False
        assert results[1]["success"] is True

    async def test_empty_configs_returns_empty(self, event):
        assert await dispatch_notifications(event, [], {}) == []
