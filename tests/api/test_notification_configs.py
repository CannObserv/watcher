"""Integration tests for notification config API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestCreateNotificationConfig:
    async def test_create_webhook_config(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Notified Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://hooks.example.com/abc"}},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["channel"] == "webhook"
        assert data["is_active"] is True

    async def test_create_config_invalid_watch(self, client):
        response = await client.post(
            "/api/watches/00000000000000000000000000/notifications",
            json={"channel": "webhook", "config": {}},
        )
        assert response.status_code == 404


class TestListNotificationConfigs:
    async def test_list_configs(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Multi Notify",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        await client.post(
            f"/api/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://a.example.com"}},
        )
        await client.post(
            f"/api/watches/{watch_id}/notifications",
            json={"channel": "slack", "config": {"webhook_url": "https://hooks.slack.com/b"}},
        )
        response = await client.get(f"/api/watches/{watch_id}/notifications")
        assert response.status_code == 200
        assert len(response.json()) == 2


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Delete Notify",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://hooks.example.com"}},
        )
        config_id = create_resp.json()["id"]
        response = await client.delete(
            f"/api/watches/{watch_id}/notifications/{config_id}"
        )
        assert response.status_code == 204
