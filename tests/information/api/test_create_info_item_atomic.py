"""Tests for atomic InfoItem creation with optional initial_info_spec."""

import pytest
from sqlalchemy import func, select

from src.information.core.models import InfoItem, InfoSpec

HEADERS = {"X-API-Key": "test-secret-key"}

VALID_SPEC_DOC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "full_page"},
    "fingerprint": {"algorithm": "simhash"},
}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_create_without_initial_spec_keeps_existing_response_shape(client):
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "no-spec"},
    )
    assert response.status_code == 201
    body = response.json()
    assert "info_item_id" in body
    assert body["name"] == "no-spec"
    # info_spec_id is null (no spec was created) — callers that ignore unknown
    # fields keep working; callers that opt into the new shape see null.
    assert body.get("info_spec_id") is None


@pytest.mark.asyncio
async def test_create_with_valid_initial_spec_returns_both_ids(client, session):
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "with-spec", "initial_info_spec": VALID_SPEC_DOC},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "with-spec"
    assert body["info_item_id"]
    assert body["info_spec_id"]

    # Exactly one InfoSpec row, priority 1, active true, linked to the new item.
    item_id = body["info_item_id"]
    spec_count = await session.scalar(
        select(func.count(InfoSpec.info_spec_id)).where(InfoSpec.info_item_id == item_id)
    )
    assert spec_count == 1
    spec = (
        await session.execute(select(InfoSpec).where(InfoSpec.info_item_id == item_id))
    ).scalar_one()
    assert spec.priority == 1
    assert spec.active is True
    assert str(spec.info_spec_id) == body["info_spec_id"]


@pytest.mark.asyncio
async def test_create_with_invalid_initial_spec_returns_422_no_rows(client, session):
    bad_doc = dict(VALID_SPEC_DOC)
    bad_doc.pop("fingerprint")
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "should-not-exist", "initial_info_spec": bad_doc},
    )
    assert response.status_code == 422

    # Neither row should exist.
    item_count = await session.scalar(
        select(func.count(InfoItem.info_item_id)).where(InfoItem.name == "should-not-exist")
    )
    assert item_count == 0
    # No spec row referencing this name's would-be ID.
    all_specs = (await session.execute(select(InfoSpec))).scalars().all()
    assert len(all_specs) == 0


@pytest.mark.asyncio
async def test_create_with_unsupported_schema_version_returns_422_no_rows(client, session):
    bad_doc = {**VALID_SPEC_DOC, "schema_version": 99}
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "x", "initial_info_spec": bad_doc},
    )
    assert response.status_code == 422
    item_count = await session.scalar(
        select(func.count(InfoItem.info_item_id)).where(InfoItem.name == "x")
    )
    assert item_count == 0
