"""GET /info-items/{id}/primary-info-spec tests."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


async def _create_item(client) -> str:
    r = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    return r.json()["info_item_id"]


async def _create_spec(client, item_id: str, priority: int | None = None) -> dict:
    payload = {"document": _doc()}
    if priority is not None:
        payload["priority"] = priority
    r = await client.post(f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS, json=payload)
    return r.json()


@pytest.mark.asyncio
async def test_primary_returns_priority_1(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)
    await _create_spec(client, item_id)  # second at priority 2
    r = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["info_spec_id"] == first["info_spec_id"]
    assert r.json()["priority"] == 1


@pytest.mark.asyncio
async def test_primary_404_when_none(client):
    item_id = await _create_item(client)
    r = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_primary_404_when_unknown_info_item(client):
    r = await client.get(
        "/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ/primary-info-spec",
        headers=HEADERS,
    )
    assert r.status_code == 404
