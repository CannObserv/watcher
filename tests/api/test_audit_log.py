"""Integration tests for audit log API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestListAuditLog:
    async def test_list_audit_entries(self, client):
        await client.post("/api/watches", json={
            "name": "Audit Test", "url": "https://example.com", "content_type": "html",
        })
        response = await client.get("/api/audit")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(e["event_type"] == "watch.created" for e in data)

    async def test_filter_by_event_type(self, client):
        await client.post("/api/watches", json={
            "name": "Event Filter", "url": "https://example.com", "content_type": "html",
        })
        response = await client.get("/api/audit?event_type=watch.created")
        assert response.status_code == 200
        assert all(e["event_type"] == "watch.created" for e in response.json())

    async def test_filter_by_watch_id(self, client):
        resp = await client.post("/api/watches", json={
            "name": "Watch Filter", "url": "https://example.com", "content_type": "html",
        })
        watch_id = resp.json()["id"]
        response = await client.get(f"/api/audit?watch_id={watch_id}")
        assert response.status_code == 200
        assert all(e["watch_id"] == watch_id for e in response.json())

    async def test_pagination(self, client):
        response = await client.get("/api/audit?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1
