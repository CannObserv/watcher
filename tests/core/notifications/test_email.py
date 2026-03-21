"""Tests for EmailChannel."""

from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import aiosmtplib

from src.core.notifications.base import ChangeEvent
from src.core.notifications.email import EmailChannel


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


_VALID_CONFIG = {
    "host": "smtp.example.com",
    "port": 465,
    "from_addr": "watcher@example.com",
    "to_addr": "user@example.com",
    "username": "watcher",
    "password": "secret",
}


class TestEmailChannel:
    """EmailChannel sends plain-text email via aiosmtplib."""

    @patch("src.core.notifications.email.aiosmtplib.send", new_callable=AsyncMock)
    async def test_sends_email_with_correct_subject(self, mock_send):
        channel = EmailChannel()
        event = _make_event(watch_name="License Page")
        result = await channel.send(event, _VALID_CONFIG)

        assert result is True
        mock_send.assert_awaited_once()
        sent_msg: EmailMessage = mock_send.call_args[0][0]
        assert "License Page" in sent_msg["Subject"]

    @patch(
        "src.core.notifications.email.aiosmtplib.send",
        new_callable=AsyncMock,
        side_effect=aiosmtplib.SMTPException("relay denied"),
    )
    async def test_returns_false_on_smtp_error(self, mock_send):
        channel = EmailChannel()
        result = await channel.send(_make_event(), _VALID_CONFIG)
        assert result is False

    async def test_missing_config_returns_false(self):
        channel = EmailChannel()
        result = await channel.send(_make_event(), {})
        assert result is False
