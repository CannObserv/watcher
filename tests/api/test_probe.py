"""Integration tests for POST /api/probe."""

import pytest

pytestmark = pytest.mark.integration


class TestProbeEndpoint:
    async def test_probe_returns_effective_url(self, client):
        # conftest mock probe returns URL as-is (no redirect)
        response = await client.post("/api/probe", json={"url": "https://example.com/page"})
        assert response.status_code == 200
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"
        assert isinstance(data["redirect_chain"], list)
        assert data["status_code"] == 200

    async def test_probe_missing_url_returns_422(self, client):
        response = await client.post("/api/probe", json={})
        assert response.status_code == 422
