"""Integration tests for POST /api/probe."""

import pytest

pytestmark = pytest.mark.integration


class TestProbeEndpoint:
    async def test_probe_returns_effective_url(self, client):
        # conftest mock probe returns URL as-is (no redirect)
        response = await client.post("/api/v1/probe", json={"url": "https://example.com/page"})
        assert response.status_code == 200
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"
        assert isinstance(data["redirect_chain"], list)
        assert data["status_code"] == 200

    async def test_probe_missing_url_returns_422(self, client):
        response = await client.post("/api/v1/probe", json={})
        assert response.status_code == 422


class TestProbeDestinationRefused:
    """A refused destination is its own 422, not 'URL unreachable' (#305)."""

    async def test_refusal_returns_422_naming_the_refusal(self, client):
        from src.api.deps import get_probe_fn
        from src.api.main import app
        from src.core.egress import DestinationRefused

        async def refusing_probe(url: str):
            raise DestinationRefused(
                "http://127.0.0.1:9999/ resolves to 127.0.0.1, inside the refused "
                "range 127.0.0.0/8"
            )

        app.dependency_overrides[get_probe_fn] = lambda: refusing_probe

        response = await client.post("/api/v1/probe", json={"url": "http://127.0.0.1:9999/"})

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "refused" in detail.lower()
        assert "unreachable" not in detail.lower()
