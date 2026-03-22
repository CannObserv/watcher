"""Integration tests for domain API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestGetDomains:
    async def test_list_domains_empty(self, client):
        response = await client.get("/api/domains")
        assert response.status_code == 200
        assert response.json() == []

    async def test_get_domain_not_found(self, client):
        response = await client.get("/api/domains/nonexistent.com")
        assert response.status_code == 404


class TestPatchDomain:
    async def test_patch_creates_domain_if_absent(self, client):
        response = await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 3.0, "max_concurrency": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "example.com"
        assert data["min_interval"] == 3.0
        assert data["max_concurrency"] == 1
        assert data["current_interval"] == 3.0  # defaults to min_interval on create

    async def test_patch_updates_existing_domain(self, client):
        await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 2.0, "max_concurrency": 2},
        )
        response = await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 5.0},
        )
        assert response.status_code == 200
        assert response.json()["min_interval"] == 5.0
        assert response.json()["max_concurrency"] == 2  # unchanged

    async def test_patch_with_no_fields_returns_current(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.patch("/api/domains/example.com", json={})
        assert response.status_code == 200
        assert response.json()["min_interval"] == 2.0

    async def test_list_includes_patched_domain(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.get("/api/domains")
        assert response.status_code == 200
        names = [d["name"] for d in response.json()]
        assert "example.com" in names

    async def test_response_includes_id(self, client):
        response = await client.patch("/api/domains/example.com", json={})
        assert "id" in response.json()


class TestGetDomainByName:
    async def test_get_existing_domain(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.get("/api/domains/example.com")
        assert response.status_code == 200
        assert response.json()["name"] == "example.com"


class TestDeleteDomain:
    async def test_delete_orphaned_domain_returns_204(self, client):
        await client.patch("/api/domains/orphan.com", json={"min_interval": 1.0})
        response = await client.delete("/api/domains/orphan.com")
        assert response.status_code == 204

    async def test_delete_nonexistent_returns_404(self, client):
        response = await client.delete("/api/domains/nope.com")
        assert response.status_code == 404

    @pytest.mark.xfail(strict=False, reason="requires probe in watch creation (Task 6)")
    async def test_delete_domain_with_watches_returns_409(self, client):
        await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        response = await client.delete("/api/domains/example.com")
        assert response.status_code == 409
        assert "watches" in response.json()["detail"].lower()
