"""Tests for BUILD_ID cache-busting integration."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_health_includes_build_id(client: AsyncClient):
    """Health endpoint includes build field."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "build" in body


@pytest.mark.anyio
async def test_build_id_in_static_asset_urls(client: AsyncClient):
    """Static asset URLs include ?v=BUILD_ID query parameter."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "?v=" in resp.text


@pytest.mark.anyio
async def test_build_id_defaults_to_dev(client: AsyncClient):
    """BUILD_ID defaults to 'dev' when env var is not set."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "?v=dev" in resp.text
