"""Integration tests for notification template CRUD API."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from src.core.notifications.dispatcher import DispatchResult

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
async def test_get_template(client: AsyncClient):
    """GET /{template_id} returns the template."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Fetch Me", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    resp = await client.get(f"/api/v1/notifications/templates/{template_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == template_id
    assert data["title"] == "Fetch Me"
    assert "apprise_url" not in data


@pytest.mark.integration
async def test_get_template_not_found(client: AsyncClient):
    """GET /{template_id} returns 404 for unknown id."""
    resp = await client.get("/api/v1/notifications/templates/01J000000000000000000000ZZ")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_patch_template_updates_title(client: AsyncClient):
    """PATCH /{template_id} updates specified fields."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Old Title", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/notifications/templates/{template_id}",
        json={"title": "New Title"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["title"] == "New Title"


@pytest.mark.integration
async def test_patch_template_updates_events(client: AsyncClient):
    """PATCH /{template_id} can update events list."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Events Test", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/notifications/templates/{template_id}",
        json={"events": ["change_detected", "watch_error"]},
    )
    assert patch_resp.status_code == 200
    assert set(patch_resp.json()["events"]) == {"change_detected", "watch_error"}


@pytest.mark.integration
async def test_unassign_template_from_watch(client: AsyncClient):
    """DELETE /{template_id}/assign/{watch_id} removes the ref."""
    watch_resp = await client.post(
        "/api/v1/watches",
        json={"name": "Unassign W", "url": "https://example.com", "content_type": "html"},
    )
    watch_id = watch_resp.json()["id"]
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Unassign T", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    await client.post(f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}")

    url = f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}"
    del_resp = await client.delete(url)
    assert del_resp.status_code == 204

    # Re-assign should work after unassign (confirms ref was removed)
    re_assign = await client.post(
        f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}"
    )
    assert re_assign.status_code == 201


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


@pytest.mark.integration
async def test_test_endpoint_returns_success(client: AsyncClient):
    """POST /test dispatches through dispatch_event and returns success/reason."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Test Me", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    dispatch_result = DispatchResult(success=True, reason="Notification sent successfully")
    with patch(
        "src.api.routes.notification_templates.dispatch_event",
        new_callable=AsyncMock,
        return_value=dispatch_result,
    ):
        resp = await client.post(f"/api/v1/notifications/templates/{template_id}/test")

    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.integration
async def test_notification_template_has_content_config_column(db_session):
    """ORM model exposes content_config field (fails until migration + model are updated)."""
    from src.core.crypto import encrypt_apprise_url
    from src.core.models.notification_template import NotificationTemplate

    tpl = NotificationTemplate(
        title="Test Template",
        apprise_url=encrypt_apprise_url("slack://T/A/B"),
        channel_hint="slack",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()
    assert tpl.content_config is None  # default null


@pytest.mark.integration
async def test_test_endpoint_returns_failure_on_dispatch_error(client: AsyncClient):
    """POST /test returns failure dict when dispatch raises, never 5xx."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Fail Me", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    with patch(
        "src.api.routes.notification_templates.dispatch_event",
        new_callable=AsyncMock,
        side_effect=Exception("boom"),
    ):
        resp = await client.post(f"/api/v1/notifications/templates/{template_id}/test")

    assert resp.status_code == 200
    assert resp.json()["success"] is False


@pytest.mark.integration
async def test_create_template_with_content_config(client: AsyncClient):
    """content_config round-trips through template create → response."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json={
            "title": "Content Config Test",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
            "content_config": {
                "default": {"include_diff_snippet": True, "diff_snippet_lines": 7},
            },
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content_config"]["default"]["include_diff_snippet"] is True
    assert data["content_config"]["default"]["diff_snippet_lines"] == 7


@pytest.mark.integration
async def test_patch_template_updates_content_config(client: AsyncClient):
    """PATCH /{template_id} with content_config updates the stored value."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json={"title": "Patch CC Test", "apprise_url": VALID_URL, "events": ["change_detected"]},
    )
    template_id = tpl_resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/notifications/templates/{template_id}",
        json={
            "content_config": {
                "default": {"include_diff_full": True},
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["content_config"]["default"]["include_diff_full"] is True
