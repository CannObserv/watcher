"""Integration tests for notification config API endpoints (Apprise v2)."""

import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.integration

# A real Apprise URL that parses correctly (json:// is always available)
VALID_URL = "json://hooks.example.com/notify"
INVALID_URL = "notaschema://whatever"


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


async def _make_watch(client):
    resp = await client.post(
        "/api/v1/watches",
        json={"name": "Test Watch", "url": "https://example.com", "content_type": "html"},
    )
    return resp.json()["id"]


class TestCreateNotificationConfig:
    async def test_create_with_valid_url(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_hint"] == "json"
        assert data["events"] == ["change_detected"]
        assert data["is_active"] is True
        # apprise_url must NOT be in response
        assert "apprise_url" not in data

    async def test_default_events_is_change_detected(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        assert resp.status_code == 201
        assert resp.json()["events"] == ["change_detected"]

    async def test_invalid_apprise_url_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": INVALID_URL},
        )
        assert resp.status_code == 422

    async def test_empty_events_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": []},
        )
        assert resp.status_code == 422

    async def test_invalid_event_type_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["nonexistent_event"]},
        )
        assert resp.status_code == 422

    async def test_invalid_watch_returns_404(self, client):
        resp = await client.post(
            "/api/v1/watches/00000000000000000000000000/notifications",
            json={"apprise_url": VALID_URL},
        )
        assert resp.status_code == 404

    async def test_multiple_events(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected", "watch_error"]},
        )
        assert resp.status_code == 201
        assert set(resp.json()["events"]) == {"change_detected", "watch_error"}


class TestListNotificationConfigs:
    async def test_list_returns_all_configs(self, client):
        watch_id = await _make_watch(client)
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected"]},
        )
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": "json://second.example.com/notify", "events": ["watch_error"]},
        )
        resp = await client.get(f"/api/v1/watches/{watch_id}/notifications")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_list_excludes_other_watch_configs(self, client):
        watch_a = await _make_watch(client)
        watch_b = await _make_watch(client)
        await client.post(
            f"/api/v1/watches/{watch_a}/notifications",
            json={"apprise_url": VALID_URL},
        )
        resp = await client.get(f"/api/v1/watches/{watch_b}/notifications")
        assert resp.json() == []


class TestPatchNotificationConfig:
    async def test_toggle_is_active(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_update_events(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"events": ["watch_error", "watch_recovered"]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["events"]) == {"watch_error", "watch_recovered"}

    async def test_patch_invalid_event_type_returns_422(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"events": ["bad_event"]},
        )
        assert resp.status_code == 422

    async def test_patch_wrong_watch_returns_404(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        other_watch_id = await _make_watch(client)
        resp = await client.patch(
            f"/api/v1/watches/{other_watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 404

    async def test_patch_apprise_url_updates_stored_url(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        new_url = "json://updated.example.com/notify"
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"apprise_url": new_url},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "apprise_url" not in data  # never exposed
        assert data["channel_hint"] == "json"  # re-derived from new URL

    async def test_patch_invalid_apprise_url_returns_422(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"apprise_url": INVALID_URL},
        )
        assert resp.status_code == 422

    async def test_patch_empty_events_returns_422(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watches/{watch_id}/notifications/{config_id}",
            json={"events": []},
        )
        assert resp.status_code == 422


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/watches/{watch_id}/notifications/{config_id}")
        assert resp.status_code == 204

    async def test_delete_wrong_watch_returns_404(self, client):
        watch_id = await _make_watch(client)
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL},
        )
        config_id = create_resp.json()["id"]
        other = await _make_watch(client)
        resp = await client.delete(f"/api/v1/watches/{other}/notifications/{config_id}")
        assert resp.status_code == 404


class TestCreateNotificationConfigFromTokens:
    async def test_create_discord_from_tokens(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={
                "plugin_schema": "discord",
                "tokens": {"webhook_id": "abc123", "webhook_token": "xyz789"},
                "events": ["change_detected"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_hint"] == "Discord"
        assert "apprise_url" not in data

    async def test_missing_required_token_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={
                "plugin_schema": "discord",
                "tokens": {"webhook_id": "abc123"},  # missing webhook_token
            },
        )
        assert resp.status_code == 422

    async def test_unknown_schema_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"plugin_schema": "notaschema", "tokens": {}},
        )
        assert resp.status_code == 422

    async def test_neither_url_nor_schema_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"events": ["change_detected"]},
        )
        assert resp.status_code == 422


class TestTestNotificationConfig:
    async def _make_config(self, client, watch_id):
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"apprise_url": VALID_URL, "events": ["change_detected"]},
        )
        return resp.json()["id"]

    def _mock_result(self, success=True, reason="ok"):
        from src.core.notifications.dispatcher import DispatchResult

        return DispatchResult(success=success, reason=reason)

    async def test_test_sends_notification_and_returns_success(self, client):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watch(client)
        config_id = await self._make_config(client, watch_id)
        with patch(
            "src.api.routes.notification_configs.dispatch_event",
            new_callable=AsyncMock,
            return_value=self._mock_result(True, "Notification sent successfully"),
        ):
            resp = await client.post(f"/api/v1/watches/{watch_id}/notifications/{config_id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "reason" in data

    async def test_test_returns_success_false_on_dispatch_failure(self, client):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watch(client)
        config_id = await self._make_config(client, watch_id)
        with patch(
            "src.api.routes.notification_configs.dispatch_event",
            new_callable=AsyncMock,
            return_value=self._mock_result(False, "Delivery failed"),
        ):
            resp = await client.post(f"/api/v1/watches/{watch_id}/notifications/{config_id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "reason" in data

    async def test_test_returns_404_for_unknown_config(self, client):
        watch_id = await _make_watch(client)
        fake_id = "01JNVAJNVAJNVAJNVAJNVAJNVA"
        resp = await client.post(f"/api/v1/watches/{watch_id}/notifications/{fake_id}/test")
        assert resp.status_code == 404

    async def test_test_returns_404_for_wrong_watch(self, client):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watch(client)
        other_id = await _make_watch(client)
        config_id = await self._make_config(client, watch_id)
        with patch(
            "src.api.routes.notification_configs.dispatch_event",
            new_callable=AsyncMock,
            return_value=self._mock_result(True),
        ):
            resp = await client.post(f"/api/v1/watches/{other_id}/notifications/{config_id}/test")
        assert resp.status_code == 404

    async def test_test_returns_success_false_on_exception(self, client):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watch(client)
        config_id = await self._make_config(client, watch_id)
        with patch(
            "src.api.routes.notification_configs.dispatch_event",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            resp = await client.post(f"/api/v1/watches/{watch_id}/notifications/{config_id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "reason" in data
