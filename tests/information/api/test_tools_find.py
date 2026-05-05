"""Tests for GET /api/v1/tools/find-info-items."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


async def _seed(client, name: str, description: str | None = None) -> str:
    body = {"name": name}
    if description is not None:
        body["description"] = description
    response = await client.post("/api/v1/info-items", headers=HEADERS, json=body)
    assert response.status_code == 201
    return response.json()["info_item_id"]


@pytest.mark.asyncio
async def test_find_info_items_matches_name_substring(client):
    await _seed(client, "Colorado licenses")
    await _seed(client, "Oregon registry")
    await _seed(client, "Colorado regulator")

    response = await client.get(
        "/api/v1/tools/find-info-items", headers=HEADERS, params={"q": "colorado"}
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2
    names = {i["name"] for i in items}
    assert names == {"Colorado licenses", "Colorado regulator"}


@pytest.mark.asyncio
async def test_find_info_items_matches_description(client):
    await _seed(client, "X", description="cannabis regulator activity")
    await _seed(client, "Y", description="alcohol board")

    response = await client.get(
        "/api/v1/tools/find-info-items", headers=HEADERS, params={"q": "cannabis"}
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["name"] == "X"


@pytest.mark.asyncio
async def test_find_info_items_case_insensitive(client):
    await _seed(client, "Colorado")
    response = await client.get(
        "/api/v1/tools/find-info-items", headers=HEADERS, params={"q": "COLO"}
    )
    assert response.status_code == 200
    assert len(response.json()) == 1


@pytest.mark.asyncio
async def test_find_info_items_respects_limit(client):
    for i in range(5):
        await _seed(client, f"alpha-{i}")
    response = await client.get(
        "/api/v1/tools/find-info-items", headers=HEADERS, params={"q": "alpha", "limit": 2}
    )
    assert response.status_code == 200
    assert len(response.json()) == 2


@pytest.mark.asyncio
async def test_find_info_items_empty_query_returns_422(client):
    response = await client.get("/api/v1/tools/find-info-items", headers=HEADERS, params={"q": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_find_info_items_missing_query_returns_422(client):
    response = await client.get("/api/v1/tools/find-info-items", headers=HEADERS)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_find_info_items_no_matches_returns_empty_list(client):
    await _seed(client, "Colorado")
    response = await client.get(
        "/api/v1/tools/find-info-items", headers=HEADERS, params={"q": "nothing-matches-this"}
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_find_info_items_requires_api_key(client):
    response = await client.get("/api/v1/tools/find-info-items", params={"q": "x"})
    assert response.status_code == 403
