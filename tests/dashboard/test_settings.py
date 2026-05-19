"""Integration tests for the /settings index page."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_settings_index_has_notifications_card(client: AsyncClient):
    """GET /settings — page content includes a Notifications card linking to /notifications."""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    # Card must link to /notifications AND show a descriptive heading
    assert 'href="/notifications"' in body
    assert "Notification Templates" in body
