"""Tests for POST /api/v1/tools/validate-info-spec."""

import pytest

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


VALID_SPEC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "full_page"},
    "fingerprint": {"algorithm": "simhash"},
}


@pytest.mark.asyncio
async def test_validate_info_spec_valid_returns_200_valid_true(client):
    response = await client.post(
        "/api/v1/tools/validate-info-spec",
        headers=HEADERS,
        json={"document": VALID_SPEC},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"valid": True, "errors": []}


@pytest.mark.asyncio
async def test_validate_info_spec_invalid_returns_200_valid_false(client):
    bad = dict(VALID_SPEC)
    bad.pop("fingerprint")
    response = await client.post(
        "/api/v1/tools/validate-info-spec",
        headers=HEADERS,
        json={"document": bad},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) >= 1
    err = body["errors"][0]
    assert "path" in err
    assert "message" in err


@pytest.mark.asyncio
async def test_validate_info_spec_unsupported_schema_version(client):
    response = await client.post(
        "/api/v1/tools/validate-info-spec",
        headers=HEADERS,
        json={"document": {**VALID_SPEC, "schema_version": 99}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("schema_version" in e["message"] for e in body["errors"])


@pytest.mark.asyncio
async def test_validate_info_spec_requires_api_key(client):
    response = await client.post(
        "/api/v1/tools/validate-info-spec",
        json={"document": VALID_SPEC},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_validate_info_spec_css_without_selector(client):
    response = await client.post(
        "/api/v1/tools/validate-info-spec",
        headers=HEADERS,
        json={
            "document": {
                **VALID_SPEC,
                "extraction": {"algorithm": "css"},
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
