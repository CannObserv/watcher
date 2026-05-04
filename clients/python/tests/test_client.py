"""Basic respx-mocked tests for InformationClient endpoints."""

import httpx
import pytest
import respx
from information_client import (
    AuthError,
    NotFound,
    ValidationError,
)

BASE_URL = "http://information.test"


def _info_item_payload(info_item_id: str = "01HZZ00000000000000000000A") -> dict:
    return {
        "info_item_id": info_item_id,
        "name": "X",
        "description": None,
        "owner": None,
        "created_at": "2026-05-04T00:00:00Z",
        "updated_at": "2026-05-04T00:00:00Z",
    }


def _info_spec_payload(
    info_spec_id: str = "01HZZ00000000000000000000B",
    priority: int = 1,
    info_item_id: str = "01HZZ00000000000000000000A",
) -> dict:
    return {
        "info_spec_id": info_spec_id,
        "info_item_id": info_item_id,
        "schema_version": 1,
        "document": {
            "schema_version": 1,
            "target": {"url": "https://example.com"},
            "extraction": {"algorithm": "css", "selector": ".x"},
            "fingerprint": {"algorithm": "sha256"},
        },
        "priority": priority,
        "active": True,
        "created_at": "2026-05-04T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_create_info_item(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/info-items").mock(
            return_value=httpx.Response(201, json=_info_item_payload())
        )
        out = await client.create_info_item(name="X")
    assert out.name == "X"


@pytest.mark.asyncio
async def test_get_info_item(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A").mock(
            return_value=httpx.Response(200, json=_info_item_payload())
        )
        out = await client.get_info_item("01HZZ00000000000000000000A")
    assert out.name == "X"
    assert str(out.info_item_id) == "01HZZ00000000000000000000A"


@pytest.mark.asyncio
async def test_list_info_items(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/info-items").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _info_item_payload("01HZZ00000000000000000000A"),
                    _info_item_payload("01HZZ00000000000000000000B"),
                ],
            )
        )
        out = await client.list_info_items()
    assert len(out) == 2
    assert out[0].name == "X"


@pytest.mark.asyncio
async def test_get_primary_info_spec(client):
    with respx.mock:
        respx.get(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
        ).mock(return_value=httpx.Response(200, json=_info_spec_payload()))
        out = await client.get_primary_info_spec("01HZZ00000000000000000000A")
    assert out.priority == 1
    assert str(out.info_spec_id) == "01HZZ00000000000000000000B"


@pytest.mark.asyncio
async def test_get_primary_info_spec_404_raises_not_found(client):
    with respx.mock:
        respx.get(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
        ).mock(return_value=httpx.Response(404, json={"detail": "InfoItem not found"}))
        with pytest.raises(NotFound):
            await client.get_primary_info_spec("01HZZ00000000000000000000A")


@pytest.mark.asyncio
async def test_get_primary_info_spec_401_raises_auth_error(client):
    with respx.mock:
        respx.get(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/primary-info-spec"
        ).mock(return_value=httpx.Response(401, json={"detail": "Unauthorized"}))
        with pytest.raises(AuthError):
            await client.get_primary_info_spec("01HZZ00000000000000000000A")


@pytest.mark.asyncio
async def test_get_primary_info_spec_422_raises_validation_error(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/info-items/bad-id/primary-info-spec").mock(
            return_value=httpx.Response(
                422,
                json={
                    "detail": [
                        {"loc": ["path", "info_item_id"], "msg": "invalid", "type": "value_error"}
                    ]
                },
            )
        )
        with pytest.raises(ValidationError):
            await client.get_primary_info_spec("bad-id")


@pytest.mark.asyncio
async def test_list_active_info_specs(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _info_spec_payload(priority=1),
                    _info_spec_payload("01HZZ00000000000000000000C", priority=2),
                ],
            )
        )
        out = await client.list_active_info_specs("01HZZ00000000000000000000A")
    assert len(out) == 2
    assert [s.priority for s in out] == [1, 2]


@pytest.mark.asyncio
async def test_list_active_info_specs_401_raises_auth_error(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs").mock(
            return_value=httpx.Response(401, json={"detail": "Unauthorized"})
        )
        with pytest.raises(AuthError):
            await client.list_active_info_specs("01HZZ00000000000000000000A")


@pytest.mark.asyncio
async def test_create_info_spec(client):
    document = {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".content"},
        "fingerprint": {"algorithm": "sha256"},
    }
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs").mock(
            return_value=httpx.Response(201, json=_info_spec_payload())
        )
        out = await client.create_info_spec("01HZZ00000000000000000000A", document=document)
    assert out.priority == 1
    assert out.active is True


@pytest.mark.asyncio
async def test_create_info_spec_422_raises_validation_error(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs").mock(
            return_value=httpx.Response(
                422,
                json={
                    "detail": [
                        {"loc": ["body", "document"], "msg": "invalid", "type": "value_error"}
                    ]
                },
            )
        )
        with pytest.raises(ValidationError):
            await client.create_info_spec("01HZZ00000000000000000000A", document={})


@pytest.mark.asyncio
async def test_patch_info_spec(client):
    with respx.mock:
        respx.patch(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs/01HZZ00000000000000000000B"
        ).mock(return_value=httpx.Response(200, json=_info_spec_payload(priority=5)))
        out = await client.patch_info_spec(
            "01HZZ00000000000000000000A",
            "01HZZ00000000000000000000B",
            priority=5,
        )
    assert out.priority == 5


@pytest.mark.asyncio
async def test_patch_info_spec_deactivate(client):
    payload = _info_spec_payload()
    payload["active"] = False
    with respx.mock:
        respx.patch(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs/01HZZ00000000000000000000B"
        ).mock(return_value=httpx.Response(200, json=payload))
        out = await client.patch_info_spec(
            "01HZZ00000000000000000000A",
            "01HZZ00000000000000000000B",
            active=False,
        )
    assert out.active is False


@pytest.mark.asyncio
async def test_patch_info_spec_404_raises_not_found(client):
    with respx.mock:
        respx.patch(
            f"{BASE_URL}/api/v1/info-items/01HZZ00000000000000000000A/info-specs/01HZZ00000000000000000000B"
        ).mock(return_value=httpx.Response(404, json={"detail": "not found"}))
        with pytest.raises(NotFound):
            await client.patch_info_spec(
                "01HZZ00000000000000000000A",
                "01HZZ00000000000000000000B",
            )
