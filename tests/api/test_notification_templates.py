"""Integration tests for the notification-template library API (#200).

Post-#200 a NotificationTemplate carries an intrinsic ``visibility`` (global /
domain / watched_item) and there are no junction tables. This route is the
scope-agnostic library surface at ``/api/v1/notifications/templates``; the
per-item convenience surface lives at ``/watched-items/{id}/notifications``.

After Phase 5 (#137), templates carry a ``remote_channel_id`` instead of an
Apprise URL.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from ulid import ULID

from src.core.notifications.notify import DispatchResult
from tests.conftest import make_watched_item


def _payload(**overrides):
    """A valid global-visibility create body. Override ``visibility`` + refs as needed."""
    payload = {
        "title": "T",
        "remote_channel_id": str(ULID()),
        "channel_hint": "json",
        "events": ["change_detected"],
        "visibility": "global",
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
    assert data["visibility"] == "global"
    assert data["domain_name"] is None
    assert data["watched_item_id"] is None
    assert "id" in data
    assert "apprise_url" not in data
    assert "is_global_default" not in data


@pytest.mark.integration
async def test_create_global_template(client: AsyncClient):
    """visibility='global' leaves both refs NULL."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Global", visibility="global"),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["visibility"] == "global"
    assert data["domain_name"] is None
    assert data["watched_item_id"] is None


@pytest.mark.integration
async def test_create_domain_template(client: AsyncClient, db_session):
    """visibility='domain' requires domain_name and forbids watched_item_id."""
    # Auto-create the Domain row (FK target) by building a WatchedItem in it.
    await make_watched_item(
        db_session,
        name="Domain seed",
        primary_url="https://dom.example.com",
        domain_name="dom.example.com",
        default_content_type="html",
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Domain T", visibility="domain", domain_name="dom.example.com"),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["visibility"] == "domain"
    assert data["domain_name"] == "dom.example.com"
    assert data["watched_item_id"] is None


@pytest.mark.integration
async def test_create_watched_item_template(client: AsyncClient, db_session):
    """visibility='watched_item' requires watched_item_id and forbids domain_name.

    Repurposed from the old ``assign``-flow test: scoping a template to an item
    is now a create with visibility='watched_item', not a junction-row assign.
    """
    wi = await make_watched_item(
        db_session,
        name="WI scope",
        primary_url="https://example.com",
        default_content_type="html",
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(
            title="Item T",
            visibility="watched_item",
            watched_item_id=str(wi.id),
        ),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["visibility"] == "watched_item"
    assert data["watched_item_id"] == str(wi.id)
    assert data["domain_name"] is None


@pytest.mark.integration
async def test_create_domain_template_unknown_domain_404(client: AsyncClient):
    """Schema shape is valid but the Domain doesn't exist → 404, not a 500 FK violation."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Domain T", visibility="domain", domain_name="nope.example.com"),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Domain not found"


@pytest.mark.integration
async def test_create_watched_item_template_unknown_item_404(client: AsyncClient):
    """Schema shape is valid but the WatchedItem doesn't exist → 404, not a 500 FK violation."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Item T", visibility="watched_item", watched_item_id=str(ULID())),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.integration
async def test_create_global_with_ref_rejected(client: AsyncClient):
    """model_validator: global must not carry a domain_name/watched_item_id (422)."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(visibility="global", domain_name="dom.example.com"),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_create_domain_without_domain_name_rejected(client: AsyncClient):
    """model_validator: domain visibility requires domain_name (422)."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(visibility="domain"),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_create_watched_item_without_id_rejected(client: AsyncClient):
    """model_validator: watched_item visibility requires watched_item_id (422)."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(visibility="watched_item"),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_create_unknown_visibility_rejected(client: AsyncClient):
    """check_visibility field validator rejects out-of-set values (422)."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(visibility="nonsense"),
    )
    assert resp.status_code == 422


@pytest.mark.integration
async def test_create_missing_title_returns_422(client: AsyncClient):
    """title is now required on the library create schema."""
    payload = _payload()
    del payload["title"]
    resp = await client.post("/api/v1/notifications/templates", json=payload)
    assert resp.status_code == 422


@pytest.mark.integration
async def test_omitted_content_config_persists_sql_null(client: AsyncClient, db_session):
    """Omitting content_config stores SQL NULL, not JSONB 'null' (#198)."""
    resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="NullCfg"),
    )
    assert resp.status_code == 201, resp.text
    tpl_id = resp.json()["id"]
    is_sql_null = (
        await db_session.execute(
            text("SELECT content_config IS NULL FROM notification_templates WHERE id = :id"),
            {"id": tpl_id},
        )
    ).scalar_one()
    assert is_sql_null is True


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
async def test_list_templates_filtered_by_visibility(client: AsyncClient, db_session):
    """GET ?visibility= filters to a single scope."""
    wi = await make_watched_item(
        db_session,
        name="Filter WI",
        primary_url="https://example.com",
        default_content_type="html",
    )
    await db_session.commit()

    await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Global F"),
    )
    await client.post(
        "/api/v1/notifications/templates",
        json=_payload(
            title="Item F",
            visibility="watched_item",
            watched_item_id=str(wi.id),
        ),
    )

    resp = await client.get("/api/v1/notifications/templates?visibility=watched_item")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    assert all(r["visibility"] == "watched_item" for r in rows)


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
async def test_patch_template_toggles_is_active(client: AsyncClient):
    """PATCH /{template_id} can flip is_active."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Toggle"),
    )
    template_id = tpl_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/notifications/templates/{template_id}",
        json={"is_active": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["is_active"] is False


# Removed: test_unassign_template_from_watched_item — the assign/unassign
# endpoints (POST/DELETE /{id}/assign/{watched_item_id}) were removed in #200.
# Scoping a template to an item is now a create with visibility='watched_item'
# (see test_create_watched_item_template).

# Removed: test_delete_template_blocked_when_refs_exist — templates are
# standalone post-#200 (no junction tables), so delete never 409s on refs.
# Replaced by test_delete_template_succeeds below, which always succeeds.


@pytest.mark.integration
async def test_delete_template_succeeds(client: AsyncClient):
    """DELETE always succeeds — templates are standalone post-#200 (no ref check)."""
    tpl_resp = await client.post(
        "/api/v1/notifications/templates",
        json=_payload(title="Unused"),
    )
    template_id = tpl_resp.json()["id"]
    resp = await client.delete(f"/api/v1/notifications/templates/{template_id}")
    assert resp.status_code == 204


@pytest.mark.integration
async def test_delete_template_not_found(client: AsyncClient):
    """DELETE /{template_id} returns 404 for unknown id."""
    resp = await client.delete("/api/v1/notifications/templates/01J000000000000000000000ZZ")
    assert resp.status_code == 404


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
            "src.api.routes.notification_templates.dispatch_via_notifier",
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
    """ORM model exposes content_config field (defaults to SQL NULL)."""
    from src.core.models.notification_template import NotificationTemplate

    tpl = NotificationTemplate(
        title="Test Template",
        channel_hint="slack",
        events=["change_detected"],
        visibility="global",
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
            "src.api.routes.notification_templates.dispatch_via_notifier",
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
