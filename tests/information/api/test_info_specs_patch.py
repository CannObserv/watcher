"""PATCH /info-items/{id}/info-specs/{spec_id} tests."""

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
async def test_patch_active_false_demotes_primary(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)  # priority=1
    second = await _create_spec(client, item_id)  # priority=2

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS,
        json={"active": False},
    )
    assert r.status_code == 200
    assert r.json()["active"] is False

    primary = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert primary.json()["info_spec_id"] == second["info_spec_id"]
    assert primary.json()["priority"] == 2  # surviving spec keeps priority 2


@pytest.mark.asyncio
async def test_patch_priority_swaps(client):
    item_id = await _create_item(client)
    _ = await _create_spec(client, item_id)
    second = await _create_spec(client, item_id)

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{second['info_spec_id']}",
        headers=HEADERS,
        json={"priority": 1},
    )
    assert r.status_code == 200
    assert r.json()["priority"] == 1

    primary = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert primary.json()["info_spec_id"] == second["info_spec_id"]


@pytest.mark.asyncio
async def test_patch_unknown_returns_404(client):
    item_id = await _create_item(client)
    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/01HZZZZZZZZZZZZZZZZZZZZZZZ",
        headers=HEADERS,
        json={"active": False},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reactivate_at_conflicting_priority_shifts_existing(client):
    """Regression: reactivating an inactive spec at a priority occupied by another
    active spec must shift the conflicting spec out, not produce a 500.
    """
    item_id = await _create_item(client)
    a = await _create_spec(client, item_id)  # priority=1 (active)
    b = await _create_spec(client, item_id)  # priority=2 (active)

    # Deactivate A (now A: inactive priority=1; B: active priority=2).
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{a['info_spec_id']}",
        headers=HEADERS,
        json={"active": False},
    )
    # Promote B to priority=1 (now A: inactive priority=1; B: active priority=1).
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{b['info_spec_id']}",
        headers=HEADERS,
        json={"priority": 1},
    )
    # Reactivate A at priority=1 — its stored priority is still 1 from before
    # deactivation, and B is now active at priority=1. The shift must run even
    # though target_priority == spec.priority, because spec is going from
    # inactive to active.
    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{a['info_spec_id']}",
        headers=HEADERS,
        json={"active": True, "priority": 1},
    )
    assert r.status_code == 200
    assert r.json()["priority"] == 1
    assert r.json()["active"] is True

    # B should have been demoted to priority=2.
    listed = (await client.get(f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS)).json()
    by_id = {s["info_spec_id"]: s["priority"] for s in listed}
    assert by_id[a["info_spec_id"]] == 1
    assert by_id[b["info_spec_id"]] == 2


@pytest.mark.asyncio
async def test_reactivate_with_default_priority_appends(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS,
        json={"active": False},
    )
    _ = await _create_spec(client, item_id)  # becomes priority 1 (active)

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS,
        json={"active": True},
    )
    assert r.status_code == 200
    assert r.json()["priority"] == 2
    assert r.json()["active"] is True
