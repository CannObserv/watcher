"""POST /info-items/{id}/info-specs tests."""

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


@pytest.mark.asyncio
async def test_create_first_info_spec_default_priority_1(client):
    item_id = await _create_item(client)
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["priority"] == 1
    assert body["active"] is True
    assert body["info_item_id"] == item_id
    assert body["schema_version"] == 1


@pytest.mark.asyncio
async def test_create_second_default_priority_2(client):
    item_id = await _create_item(client)
    await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    assert r.json()["priority"] == 2


@pytest.mark.asyncio
async def test_create_explicit_priority_demotes_existing(client):
    item_id = await _create_item(client)
    first = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    first_id = first.json()["info_spec_id"]

    second = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc(), "priority": 1},
    )
    assert second.status_code == 201
    assert second.json()["priority"] == 1

    # Original first should now be priority 2
    list_r = await client.get(f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS)
    by_id = {s["info_spec_id"]: s["priority"] for s in list_r.json()}
    assert by_id[first_id] == 2


@pytest.mark.asyncio
async def test_invalid_document_returns_422(client):
    item_id = await _create_item(client)
    bad_doc = {"schema_version": 1}  # missing target, extraction, fingerprint
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": bad_doc},
    )
    assert r.status_code == 422
    assert "InfoSpec invalid" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_for_unknown_info_item_returns_404(client):
    r = await client.post(
        "/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    assert r.status_code == 404
