"""Integration tests for Watch CRUD API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client):
        response = await client.post("/api/watches", json={
            "name": "Test Watch",
            "url": "https://example.com/page",
            "content_type": "html",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["url"] == "https://example.com/page"
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data

    async def test_create_watch_with_config(self, client):
        response = await client.post("/api/watches", json={
            "name": "PDF Watch",
            "url": "https://example.com/report.pdf",
            "content_type": "pdf",
            "fetch_config": {"timeout": 30},
            "schedule_config": {"interval": "6h"},
        })
        assert response.status_code == 201
        data = response.json()
        assert data["fetch_config"] == {"timeout": 30}

    async def test_create_watch_invalid_content_type(self, client):
        response = await client.post("/api/watches", json={
            "name": "Bad",
            "url": "https://example.com",
            "content_type": "invalid",
        })
        assert response.status_code == 422


class TestListWatches:
    async def test_list_watches_empty(self, client):
        response = await client.get("/api/watches")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_watches_returns_created(self, client):
        await client.post("/api/watches", json={
            "name": "Watch 1",
            "url": "https://example.com/1",
            "content_type": "html",
        })
        response = await client.get("/api/watches")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Watch 1"


class TestGetWatch:
    async def test_get_watch_by_id(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Get Me",
            "url": "https://example.com/get",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Original",
            "url": "https://example.com/orig",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.patch(f"/api/watches/{watch_id}", json={
            "name": "Updated",
        })
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"
        assert response.json()["url"] == "https://example.com/orig"

    async def test_update_watch_not_found(self, client):
        response = await client.patch(
            "/api/watches/00000000000000000000000000",
            json={"name": "Nope"},
        )
        assert response.status_code == 404


class TestDeactivateWatch:
    async def test_deactivate_watch(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Deactivate Me",
            "url": "https://example.com/deact",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.post(f"/api/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_deactivate_watch_not_found(self, client):
        response = await client.post("/api/watches/00000000000000000000000000/deactivate")
        assert response.status_code == 404
