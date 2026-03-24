"""Tests for BUILD_ID cache-busting integration."""

import pytest

pytestmark = pytest.mark.integration


class TestBuildId:
    async def test_health_includes_build_id(self, client):
        """Health endpoint includes build field."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "build" in body

    async def test_build_id_in_static_asset_urls(self, client):
        """Static asset URLs include ?v=BUILD_ID query parameter."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "?v=" in resp.text

    async def test_build_id_defaults_to_dev(self, client):
        """BUILD_ID defaults to 'dev' when env var is not set."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "?v=dev" in resp.text
