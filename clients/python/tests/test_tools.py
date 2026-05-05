"""Tests for the authoring-tool wrappers in information_client.tools."""

import httpx
import pytest
import respx

BASE_URL = "http://information.test"

VALID_DOC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "full_page"},
    "fingerprint": {"algorithm": "simhash"},
}


@pytest.mark.asyncio
async def test_validate_info_spec_valid(client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/api/v1/tools/validate-info-spec").mock(
            return_value=httpx.Response(200, json={"valid": True, "errors": []})
        )
        result = await client.validate_info_spec(VALID_DOC)
    assert route.called
    assert result.valid is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_validate_info_spec_invalid_returns_structured_errors(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/tools/validate-info-spec").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": False,
                    "errors": [
                        {"path": ["fingerprint"], "message": "'fingerprint' is a required property"}
                    ],
                },
            )
        )
        result = await client.validate_info_spec({})
    assert result.valid is False
    assert len(result.errors) == 1
    assert result.errors[0].path == ["fingerprint"]
    assert "fingerprint" in result.errors[0].message


@pytest.mark.asyncio
async def test_validate_info_spec_sends_document_in_body(client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/api/v1/tools/validate-info-spec").mock(
            return_value=httpx.Response(200, json={"valid": True, "errors": []})
        )
        await client.validate_info_spec(VALID_DOC)
    sent_body = route.calls[0].request.read()
    assert b'"document"' in sent_body
    assert b'"schema_version"' in sent_body
