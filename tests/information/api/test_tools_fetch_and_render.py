"""Tests for POST /api/v1/tools/fetch-and-render."""

import httpx
import pytest

from src.information.api.deps import get_http_fetcher
from src.information.api.main import app
from src.information.core.tools.fetch_and_render import HttpFetcherProtocol

HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _override_with_response(response: httpx.Response) -> None:
    """Inject a stub HttpFetcher that returns ``response`` for any URL."""

    class _StubFetcher:
        async def fetch(self, url: str, config: dict | None = None):
            from src.core.fetchers.base import FetchResult

            return FetchResult(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers),
                duration_ms=12,
                fetcher_used="http",
            )

    app.dependency_overrides[get_http_fetcher] = lambda: _StubFetcher()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_http_fetcher, None)


@pytest.mark.asyncio
async def test_fetch_and_render_returns_body_and_headers(client):
    _override_with_response(
        httpx.Response(200, content=b"<html>hi</html>", headers={"content-type": "text/html"})
    )
    response = await client.post(
        "/api/v1/tools/fetch-and-render",
        headers=HEADERS,
        json={"url": "https://example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    # HttpUrl normalises bare-domain URLs with a trailing slash.
    assert body["url"] == "https://example.com/"
    assert body["status_code"] == 200
    assert body["body"] == "<html>hi</html>"
    assert body["body_bytes_total"] == len(b"<html>hi</html>")
    assert body["truncated"] is False
    assert body["screenshot_url"] is None
    assert body["headers"]["content-type"] == "text/html"


@pytest.mark.asyncio
async def test_fetch_and_render_truncates_large_bodies(client):
    big = b"a" * (6 * 1024 * 1024)  # 6 MiB
    _override_with_response(httpx.Response(200, content=big))
    response = await client.post(
        "/api/v1/tools/fetch-and-render",
        headers=HEADERS,
        json={"url": "https://example.com/big"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert len(body["body"].encode()) <= 5 * 1024 * 1024
    assert body["body_bytes_total"] == len(big)


@pytest.mark.asyncio
async def test_fetch_and_render_render_true_returns_501(client):
    response = await client.post(
        "/api/v1/tools/fetch-and-render",
        headers=HEADERS,
        json={"url": "https://example.com", "render": True},
    )
    assert response.status_code == 501
    assert "Playwright" in response.json()["detail"]


@pytest.mark.asyncio
async def test_fetch_and_render_requires_api_key(client):
    response = await client.post(
        "/api/v1/tools/fetch-and-render",
        json={"url": "https://example.com"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_fetch_and_render_passes_url_to_fetcher(client):
    captured = {}

    class _SpyFetcher:
        async def fetch(self, url: str, config: dict | None = None):
            from src.core.fetchers.base import FetchResult

            captured["url"] = url
            return FetchResult(
                content=b"ok", status_code=200, headers={}, duration_ms=1, fetcher_used="http"
            )

    app.dependency_overrides[get_http_fetcher] = lambda: _SpyFetcher()
    await client.post(
        "/api/v1/tools/fetch-and-render",
        headers=HEADERS,
        json={"url": "https://target.example.com/path"},
    )
    # The route forwards the (normalised) URL to the fetcher unchanged.
    assert captured["url"] == "https://target.example.com/path"


def test_protocol_marker_exported():
    """Smoke check that the protocol type is importable for type-only callers."""
    assert HttpFetcherProtocol is not None
