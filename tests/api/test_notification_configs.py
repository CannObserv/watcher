"""Integration tests for notification config API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestCreateNotificationConfig:
    async def test_create_webhook_config(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Notified Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://hooks.example.com/abc"}},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["channel"] == "webhook"
        assert data["is_active"] is True

    async def test_create_config_invalid_watch(self, client):
        response = await client.post(
            "/api/v1/watches/00000000000000000000000000/notifications",
            json={"channel": "webhook", "config": {}},
        )
        assert response.status_code == 404

    async def test_create_webhook_invalid_config_returns_422(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "not-a-url"}},
        )
        assert response.status_code == 422

    async def test_create_webhook_missing_url_returns_422(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W2", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {}},
        )
        assert response.status_code == 422

    async def test_create_slack_invalid_config_returns_422(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W3", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "slack", "config": {"webhook_url": "not-a-url"}},
        )
        assert response.status_code == 422

    async def test_create_email_invalid_config_returns_422(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W4", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "email", "config": {"host": "smtp.example.com"}},
        )
        assert response.status_code == 422

    async def test_create_unknown_channel_returns_422(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W5", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "telegram", "config": {}},
        )
        assert response.status_code == 422

    async def test_create_email_valid_config_succeeds(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={"name": "W6", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={
                "channel": "email",
                "config": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "from_addr": "from@example.com",
                    "to_addr": "to@example.com",
                },
            },
        )
        assert response.status_code == 201


class TestListNotificationConfigs:
    async def test_list_configs(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Multi Notify",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://a.example.com"}},
        )
        await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "slack", "config": {"webhook_url": "https://hooks.slack.com/b"}},
        )
        response = await client.get(f"/api/v1/watches/{watch_id}/notifications")
        assert response.status_code == 200
        assert len(response.json()) == 2


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client):
        watch_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Delete Notify",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"channel": "webhook", "config": {"url": "https://hooks.example.com"}},
        )
        config_id = create_resp.json()["id"]
        response = await client.delete(f"/api/v1/watches/{watch_id}/notifications/{config_id}")
        assert response.status_code == 204
