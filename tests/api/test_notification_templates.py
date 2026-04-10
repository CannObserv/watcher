"""Integration tests for notification template CRUD API."""

import pytest
from httpx import AsyncClient

VALID_URL = "json://hooks.example.com/notify"


@pytest.mark.integration
async def test_create_template(client: AsyncClient):
    resp = await client.post(
        "/api/v1/notifications/templates",
        json={
            "title": "Ops Slack",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
            "is_global_default": False,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Ops Slack"
    assert "id" in data
    assert "apprise_url" not in data  # never exposed


@pytest.mark.integration
async def test_list_templates(client: AsyncClient):
    await client.post(
        "/api/v1/notifications/templates",
        json={
            "title": "Template A",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
    )
    resp = await client.get("/api/v1/notifications/templates")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.integration
async def test_delete_template_blocked_when_refs_exist(client: AsyncClient):
    """Cannot delete a template that is referenced by a watch."""
    watch_resp = await client.post(
        "/api/v1/watches",
        json={
            "name": "W",
            "url": "https://example.com",
            "content_type": "html",
        },
    )
    watch_id = watch_resp.json()["id"]

    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={
            "title": "T",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
    )
    template_id = tpl_resp.json()["id"]
    await client.post(f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}")

    del_resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert del_resp.status_code == 409


@pytest.mark.integration
async def test_delete_template_succeeds_when_no_refs(client: AsyncClient):
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={
            "title": "Unused",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
    )
    template_id = tpl_resp.json()["id"]
    resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert resp.status_code == 204
