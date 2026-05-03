"""X-API-Key bearer auth tests for the Information service."""

import pytest


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_missing_key_returns_403(client):
    response = await client.get("/api/v1/info-items")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invalid_key_returns_401(client):
    response = await client.get("/api/v1/info-items", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_passes(client):
    response = await client.get("/api/v1/info-items", headers={"X-API-Key": "test-secret-key"})
    assert response.status_code == 200  # empty list
