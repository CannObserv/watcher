"""Path-param ULID validation tests — malformed ULIDs return 422, not 404."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_get_info_item_with_malformed_ulid_returns_422(client):
    r = await client.get("/api/v1/info-items/not-a-ulid", headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_info_spec_with_malformed_item_id_returns_422(client):
    r = await client.post(
        "/api/v1/info-items/not-a-ulid/info-specs",
        headers=HEADERS,
        json={
            "document": {
                "schema_version": 1,
                "target": {"url": "https://example.com"},
                "extraction": {"algorithm": "css", "selector": ".x"},
                "fingerprint": {"algorithm": "sha256"},
            }
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_info_specs_with_malformed_item_id_returns_422(client):
    r = await client.get("/api/v1/info-items/not-a-ulid/info-specs", headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_primary_with_malformed_item_id_returns_422(client):
    r = await client.get("/api/v1/info-items/not-a-ulid/primary-info-spec", headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_with_malformed_spec_id_returns_422(client):
    item = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    item_id = item.json()["info_item_id"]
    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/not-a-ulid",
        headers=HEADERS,
        json={"active": False},
    )
    assert r.status_code == 422
