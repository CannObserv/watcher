"""Integration tests for the /settings index page."""

import re

import pytest
from httpx import AsyncClient
from ulid import ULID

from src.core.models.notification_template import NotificationTemplate

pytestmark = pytest.mark.integration


async def test_settings_index_has_notifications_card(client: AsyncClient):
    """GET /settings — page content includes a Notifications card linking to /notifications."""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    assert 'href="/notifications"' in body
    assert "Notification Templates" in body


async def test_settings_notifications_card_shows_template_count(client: AsyncClient, db_session):
    """GET /settings — Notifications card shows count of active templates."""
    for i in range(11):
        db_session.add(
            NotificationTemplate(
                title=f"T{i}",
                remote_channel_id=str(ULID()),
                channel_hint="json",
                events=["change_detected"],
            )
        )
    await db_session.flush()

    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    # Extract the notifications card block and verify count + label
    card_match = re.search(r'href="/notifications"(.*?)</a>', body, re.DOTALL)
    assert card_match, "No notifications card found on settings page"
    card = card_match.group(1)
    assert "11" in card
    assert "active" in card
