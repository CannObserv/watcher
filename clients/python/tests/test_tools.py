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


def _info_item_payload(info_item_id: str, name: str) -> dict:
    return {
        "info_item_id": info_item_id,
        "name": name,
        "description": None,
        "owner": None,
        "created_at": "2026-05-04T00:00:00Z",
        "updated_at": "2026-05-04T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_find_info_item_returns_typed_list(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/tools/find-info-items").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _info_item_payload("01HZZ00000000000000000000A", "Colorado licenses"),
                    _info_item_payload("01HZZ00000000000000000000B", "Colorado regulator"),
                ],
            )
        )
        results = await client.find_info_item("colorado")
    assert len(results) == 2
    assert results[0].name == "Colorado licenses"
    assert results[1].name == "Colorado regulator"


@pytest.mark.asyncio
async def test_find_info_item_passes_query_and_limit(client):
    with respx.mock:
        route = respx.get(f"{BASE_URL}/api/v1/tools/find-info-items").mock(
            return_value=httpx.Response(200, json=[])
        )
        await client.find_info_item("alpha", limit=5)
    sent_url = str(route.calls[0].request.url)
    assert "q=alpha" in sent_url
    assert "limit=5" in sent_url


@pytest.mark.asyncio
async def test_find_info_item_empty_result(client):
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/tools/find-info-items").mock(
            return_value=httpx.Response(200, json=[])
        )
        results = await client.find_info_item("nothing")
    assert results == []


@pytest.mark.asyncio
async def test_create_info_item_atomic_returns_with_spec_result(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/info-items").mock(
            return_value=httpx.Response(
                201,
                json={
                    "info_item_id": "01HZZ00000000000000000000A",
                    "info_spec_id": "01HZZ00000000000000000000B",
                    "name": "X",
                    "description": "desc",
                    "owner": None,
                    "created_at": "2026-05-04T00:00:00Z",
                    "updated_at": "2026-05-04T00:00:00Z",
                },
            )
        )
        result = await client.create_info_item(
            name="X",
            description="desc",
            initial_info_spec=VALID_DOC,
        )
    assert result.info_item_id == "01HZZ00000000000000000000A"
    assert result.info_spec_id == "01HZZ00000000000000000000B"
    assert result.name == "X"
    assert result.description == "desc"


@pytest.mark.asyncio
async def test_create_info_item_atomic_sends_initial_info_spec(client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/api/v1/info-items").mock(
            return_value=httpx.Response(
                201,
                json={
                    "info_item_id": "01HZZ00000000000000000000A",
                    "info_spec_id": "01HZZ00000000000000000000B",
                    "name": "X",
                    "description": None,
                    "owner": None,
                    "created_at": "2026-05-04T00:00:00Z",
                    "updated_at": "2026-05-04T00:00:00Z",
                },
            )
        )
        await client.create_info_item(name="X", initial_info_spec=VALID_DOC)
    sent_body = route.calls[0].request.read()
    assert b'"initial_info_spec"' in sent_body
    assert b'"schema_version"' in sent_body


@pytest.mark.asyncio
async def test_fetch_and_render_returns_typed_result(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/tools/fetch-and-render").mock(
            return_value=httpx.Response(
                200,
                json={
                    "url": "https://example.com/",
                    "status_code": 200,
                    "headers": {"content-type": "text/html"},
                    "body": "<html>hi</html>",
                    "body_bytes_total": 16,
                    "truncated": False,
                    "screenshot_url": None,
                },
            )
        )
        result = await client.fetch_and_render("https://example.com")
    assert result.status_code == 200
    assert result.body == "<html>hi</html>"
    assert result.truncated is False
    assert result.screenshot_url is None
    assert result.headers["content-type"] == "text/html"


@pytest.mark.asyncio
async def test_fetch_and_render_passes_render_flag(client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/api/v1/tools/fetch-and-render").mock(
            return_value=httpx.Response(
                200,
                json={
                    "url": "https://example.com/",
                    "status_code": 200,
                    "headers": {},
                    "body": "",
                    "body_bytes_total": 0,
                    "truncated": False,
                    "screenshot_url": None,
                },
            )
        )
        await client.fetch_and_render("https://example.com", render=False)
    sent_body = route.calls[0].request.read()
    assert b'"render": false' in sent_body or b'"render":false' in sent_body


@pytest.mark.asyncio
async def test_preview_extraction_returns_typed_result(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/tools/preview-extraction").mock(
            return_value=httpx.Response(
                200,
                json={
                    "chunks": [
                        {
                            "index": 0,
                            "chunk_type": "page",
                            "label": "page-1",
                            "text": "kept",
                            "char_count": 4,
                        }
                    ],
                    "total_chars": 4,
                    "fingerprint_algorithm": "simhash",
                    "computed_fingerprint": "12345",
                },
            )
        )
        result = await client.preview_extraction("https://example.com", VALID_DOC)
    assert len(result.chunks) == 1
    assert result.chunks[0].text == "kept"
    assert result.chunks[0].char_count == 4
    assert result.total_chars == 4
    assert result.fingerprint_algorithm == "simhash"
    assert result.computed_fingerprint == "12345"


@pytest.mark.asyncio
async def test_propose_selectors_returns_typed_candidates(client):
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/tools/propose-selectors").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "selector": "h1.page-title",
                        "sample_text": "Active Cannabis Licenses",
                        "stability_score": 0.85,
                    },
                    {
                        "selector": "div.hash-abc12345xyz",
                        "sample_text": "Active Cannabis Licenses",
                        "stability_score": 0.2,
                    },
                ],
            )
        )
        results = await client.propose_selectors("https://example.com", "Active Cannabis Licenses")
    assert len(results) == 2
    assert results[0].selector == "h1.page-title"
    assert results[0].stability_score == 0.85
    assert results[1].stability_score == 0.2


@pytest.mark.asyncio
async def test_propose_selectors_passes_top_k(client):
    with respx.mock:
        route = respx.post(f"{BASE_URL}/api/v1/tools/propose-selectors").mock(
            return_value=httpx.Response(200, json=[])
        )
        await client.propose_selectors("https://example.com", "x", top_k=3)
    sent_body = route.calls[0].request.read()
    assert b'"top_k": 3' in sent_body or b'"top_k":3' in sent_body


@pytest.mark.asyncio
async def test_create_info_item_without_initial_spec_uses_legacy_path(client):
    """Backwards-compat: omitting initial_info_spec routes through generated client."""
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/info-items").mock(
            return_value=httpx.Response(
                201,
                json={
                    "info_item_id": "01HZZ00000000000000000000A",
                    "name": "X",
                    "description": None,
                    "owner": None,
                    "created_at": "2026-05-04T00:00:00Z",
                    "updated_at": "2026-05-04T00:00:00Z",
                },
            )
        )
        result = await client.create_info_item(name="X")
    # Returned object is the generated InfoItemOut (no info_spec_id attr).
    assert result.name == "X"
    assert not hasattr(result, "info_spec_id")
