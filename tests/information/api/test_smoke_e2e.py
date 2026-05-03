"""End-to-end smoke test — exercises the full Phase 1 contract."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc(url: str = "https://example.com") -> dict:
    return {
        "schema_version": 1,
        "target": {"url": url},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


@pytest.mark.asyncio
async def test_full_phase1_round_trip(client):
    # 1. Create an InfoItem
    item_resp = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "Colorado active licenses", "owner": "greg"},
    )
    assert item_resp.status_code == 201
    item_id = item_resp.json()["info_item_id"]

    # 2. Create primary InfoSpec
    primary_resp = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc("https://example.com/primary")},
    )
    assert primary_resp.status_code == 201
    primary_id = primary_resp.json()["info_spec_id"]

    # 3. GET primary returns it
    p = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert p.json()["info_spec_id"] == primary_id

    # 4. Add a fallback at priority 2
    fb_resp = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc("https://example.com/fallback")},
    )
    fallback_id = fb_resp.json()["info_spec_id"]
    assert fb_resp.json()["priority"] == 2

    # 5. List returns both, ordered
    list_resp = await client.get(f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS)
    listed = list_resp.json()
    assert [s["info_spec_id"] for s in listed] == [primary_id, fallback_id]

    # 6. Deactivate the primary → fallback becomes the new primary
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{primary_id}",
        headers=HEADERS,
        json={"active": False},
    )
    p2 = await client.get(f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS)
    assert p2.json()["info_spec_id"] == fallback_id
