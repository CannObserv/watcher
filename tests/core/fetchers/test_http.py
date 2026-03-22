"""Tests for HTTP fetcher."""

import httpx
import pytest

from src.core.fetchers.base import FetchResult
from src.core.fetchers.http import HttpFetcher


class TestFetchResult:
    def test_create_result(self):
        result = FetchResult(
            content=b"<html>hello</html>",
            status_code=200,
            headers={"content-type": "text/html"},
            duration_ms=150,
            fetcher_used="http",
        )
        assert result.content == b"<html>hello</html>"
        assert result.status_code == 200
        assert result.duration_ms == 150

    def test_is_success(self):
        result = FetchResult(
            content=b"ok", status_code=200, headers={}, duration_ms=100, fetcher_used="http"
        )
        assert result.is_success is True

    def test_is_not_success(self):
        result = FetchResult(
            content=b"", status_code=404, headers={}, duration_ms=100, fetcher_used="http"
        )
        assert result.is_success is False


class TestHttpFetcher:
    @pytest.mark.integration
    async def test_fetch_real_url(self):
        fetcher = HttpFetcher()
        result = await fetcher.fetch("https://httpbin.org/html")
        assert result.is_success
        assert len(result.content) > 0

    async def test_fetch_with_mock_client(self):
        mock_response = httpx.Response(
            200,
            content=b"<html>test</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fetcher = HttpFetcher(client=mock_client)
        result = await fetcher.fetch("https://example.com")
        assert result.is_success
        assert result.content == b"<html>test</html>"

    async def test_fetch_records_duration(self):
        mock_response = httpx.Response(
            200, content=b"ok", request=httpx.Request("GET", "https://example.com")
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fetcher = HttpFetcher(client=mock_client)
        result = await fetcher.fetch("https://example.com")
        assert result.duration_ms >= 0

    async def test_fetch_passes_custom_headers(self):
        captured = {}

        def handler(request):
            captured.update(dict(request.headers))
            return httpx.Response(200, content=b"ok", request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = HttpFetcher(client=mock_client)
        await fetcher.fetch("https://example.com", config={"headers": {"X-Custom": "test"}})
        assert captured.get("x-custom") == "test"
