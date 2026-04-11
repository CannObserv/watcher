"""Integration tests for the /notifications dashboard page (template library CRUD UI)."""

import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"


@pytest.mark.integration
async def test_notifications_page_loads(client: AsyncClient):
    resp = await client.get("/notifications")
    assert resp.status_code == 200
    assert b"Notification" in resp.content


@pytest.mark.integration
async def test_create_template_via_dashboard_form(client: AsyncClient, db_session):
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Ops Alert",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200

    from sqlalchemy import select

    from src.core.models.notification_template import NotificationTemplate

    result = await db_session.execute(
        select(NotificationTemplate).where(NotificationTemplate.title == "Ops Alert")
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.integration
async def test_create_template_invalid_url_returns_form_with_error(client: AsyncClient):
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Bad",
            "apprise_url": "not-a-valid-apprise-url",
            "events": ["change_detected"],
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # Returns the add-row form again with an error message
    assert b"error" in resp.content.lower() or b"invalid" in resp.content.lower()
