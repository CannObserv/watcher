"""Tests for POST /api/v1/tools/preview-extraction."""

import httpx
import pytest

from src.core.fetchers.base import FetchResult
from src.information.api.deps import get_http_fetcher
from src.information.api.main import app

HEADERS = {"X-API-Key": "test-secret-key"}

VALID_FULL_PAGE_DOC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "full_page"},
    "fingerprint": {"algorithm": "simhash"},
}

VALID_CSS_DOC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "css", "selector": ".target"},
    "fingerprint": {"algorithm": "sha256"},
}

HTML_FIXTURE = (
    b"<html><body><div class='target'>kept content</div><div>dropped content</div></body></html>"
)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _stub_fetcher(content: bytes = HTML_FIXTURE, *, raise_exc: Exception | None = None):
    class _Stub:
        async def fetch(self, url: str, config: dict | None = None):
            if raise_exc is not None:
                raise raise_exc
            return FetchResult(
                content=content,
                status_code=200,
                headers={"content-type": "text/html"},
                duration_ms=5,
                fetcher_used="http",
            )

    return _Stub()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_http_fetcher, None)


@pytest.mark.asyncio
async def test_preview_extraction_full_page_returns_chunks_and_simhash(client):
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher()
    response = await client.post(
        "/api/v1/tools/preview-extraction",
        headers=HEADERS,
        json={"url": "https://example.com", "document": VALID_FULL_PAGE_DOC},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["chunks"]) >= 1
    joined = " ".join(c["text"] for c in body["chunks"])
    assert "kept content" in joined
    assert body["total_chars"] > 0
    assert body["fingerprint_algorithm"] == "simhash"
    # simhash is rendered as a decimal int; non-empty + parseable.
    assert isinstance(body["computed_fingerprint"], str)
    int(body["computed_fingerprint"])  # raises if not parseable


@pytest.mark.asyncio
async def test_preview_extraction_css_filters_to_selector(client):
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher()
    response = await client.post(
        "/api/v1/tools/preview-extraction",
        headers=HEADERS,
        json={"url": "https://example.com", "document": VALID_CSS_DOC},
    )
    assert response.status_code == 200
    body = response.json()
    joined = " ".join(c["text"] for c in body["chunks"])
    assert "kept content" in joined
    assert "dropped content" not in joined
    assert body["fingerprint_algorithm"] == "sha256"
    # sha256 hex digest is exactly 64 chars.
    assert len(body["computed_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_preview_extraction_invalid_spec_returns_422_with_errors(client):
    bad_doc = dict(VALID_FULL_PAGE_DOC)
    bad_doc.pop("fingerprint")
    response = await client.post(
        "/api/v1/tools/preview-extraction",
        headers=HEADERS,
        json={"url": "https://example.com", "document": bad_doc},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert isinstance(detail["errors"], list)
    assert len(detail["errors"]) >= 1


@pytest.mark.asyncio
async def test_preview_extraction_unreachable_target_returns_422_target_unreachable(client):
    app.dependency_overrides[get_http_fetcher] = lambda: _stub_fetcher(
        raise_exc=httpx.ConnectError("nope")
    )
    response = await client.post(
        "/api/v1/tools/preview-extraction",
        headers=HEADERS,
        json={"url": "https://unreachable.example.com", "document": VALID_FULL_PAGE_DOC},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "target_unreachable"


@pytest.mark.asyncio
async def test_preview_extraction_requires_api_key(client):
    response = await client.post(
        "/api/v1/tools/preview-extraction",
        json={"url": "https://example.com", "document": VALID_FULL_PAGE_DOC},
    )
    assert response.status_code == 403
