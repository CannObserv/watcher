"""InfoItem CRUD route tests."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_create_info_item(client):
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "Colorado active licenses", "owner": "greg"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Colorado active licenses"
    assert body["owner"] == "greg"
    assert body["description"] is None
    assert len(body["info_item_id"]) == 26  # ULID length


@pytest.mark.asyncio
async def test_get_info_item(client):
    create = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    item_id = create.json()["info_item_id"]
    get = await client.get(f"/api/v1/info-items/{item_id}", headers=HEADERS)
    assert get.status_code == 200
    assert get.json()["info_item_id"] == item_id


@pytest.mark.asyncio
async def test_get_info_item_404(client):
    response = await client.get("/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ", headers=HEADERS)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_info_items_empty(client):
    response = await client.get("/api/v1/info-items", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == []
