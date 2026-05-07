"""Integration tests for notification template CRUD API.

After Phase 5 (#137), templates carry a `remote_channel_id` instead of an
Apprise URL.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from ulid import ULID

from src.core.notifications.notify import DispatchResult
from tests.conftest import make_watch


def _payload(**overrides):
    payload = {
        "title": "T",
        "remote_channel_id": str(ULID()),
        "channel_hint": "json",
        "events": ["change_detected"],
        "is_global_default": False,
    }
    payload.update(overrides)
    return payload


def _patch_notifier_client():
    client_mock = AsyncMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "src.api.routes.notification_templates.get_notifier_client",
        return_value=client_mock,
    )


@pytest.mark.integration
async def test_create_template(client: AsyncClient):
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Ops Slack"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Ops Slack"
    assert "id" in data
    assert "apprise_url" not in data


@pytest.mark.integration
async def test_list_templates(client: AsyncClient):
    await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Template A"),
    )
    resp = await client.get("/api/v1/notifications/templates")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.integration
async def test_get_template(client: AsyncClient):
    """GET /{template_id} returns the template."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Fetch Me"),
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
        json=_payload(title="Old Title"),
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
        json=_payload(title="Events Test"),
    )
    template_id = tpl_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/notifications/templates/{template_id}",
        json={"events": ["change_detected", "watch_error"]},
    )
    assert patch_resp.status_code == 200
    assert set(patch_resp.json()["events"]) == {"change_detected", "watch_error"}


@pytest.mark.integration
async def test_unassign_template_from_watch(client: AsyncClient, db_session):
    """DELETE /{template_id}/assign/{watch_id} removes the ref."""
    _watch_obj = await make_watch(
        db_session, name="Unassign W", url="https://example.com", content_type="html"
    )
    await db_session.commit()

    watch_id = str(_watch_obj.id)
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Unassign T"),
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
async def test_delete_template_blocked_when_refs_exist(client: AsyncClient, db_session):
    """Cannot delete a template that is referenced by a watch."""
    _watch_obj = await make_watch(
        db_session, name="W", url="https://example.com", content_type="html"
    )
    await db_session.commit()

    watch_id = str(_watch_obj.id)
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(),
    )
    template_id = tpl_resp.json()["id"]
    await client.post(f"/api/v1/notifications/templates/{template_id}/assign/{watch_id}")

    del_resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert del_resp.status_code == 409


@pytest.mark.integration
async def test_delete_template_succeeds_when_no_refs(client: AsyncClient):
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Unused"),
    )
    template_id = tpl_resp.json()["id"]
    resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert resp.status_code == 204


@pytest.mark.integration
async def test_test_endpoint_returns_success(client: AsyncClient):
    """POST /test dispatches through the notifier client and returns success/reason."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Test Me"),
    )
    template_id = tpl_resp.json()["id"]

    dispatch_result = DispatchResult(success=True, reason="Notification sent successfully")
    with (
        patch(
            "src.api.routes.notification_templates._dispatch_via_notifier",
            new_callable=AsyncMock,
            return_value=dispatch_result,
        ),
        _patch_notifier_client(),
    ):
        resp = await client.post(f"/api/v1/notifications/templates/{template_id}/test")

    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.integration
async def test_notification_template_has_content_config_column(db_session):
    """ORM model exposes content_config field."""
    from src.core.models.notification_template import NotificationTemplate

    tpl = NotificationTemplate(
        title="Test Template",
        channel_hint="slack",
        events=["change_detected"],
        remote_channel_id=str(ULID()),
    )
    db_session.add(tpl)
    await db_session.flush()
    assert tpl.content_config is None  # default null


@pytest.mark.integration
async def test_test_endpoint_returns_failure_on_dispatch_error(client: AsyncClient):
    """POST /test returns failure dict when dispatch raises, never 5xx."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Fail Me"),
    )
    template_id = tpl_resp.json()["id"]

    with (
        patch(
            "src.api.routes.notification_templates._dispatch_via_notifier",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ),
        _patch_notifier_client(),
    ):
        resp = await client.post(f"/api/v1/notifications/templates/{template_id}/test")

    assert resp.status_code == 200
    assert resp.json()["success"] is False


@pytest.mark.integration
async def test_create_template_with_content_config(client: AsyncClient):
    """content_config round-trips through template create → response."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(
            title="Content Config Test",
            content_config={
                "default": {"include_diff_snippet": True, "diff_snippet_lines": 7},
            },
        ),
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
        json=_payload(title="Patch CC Test"),
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
