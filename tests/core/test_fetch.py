"""Tests for the watcher fetch adapter over co-core's async fetch effect (#236)."""

import httpx
import pytest

from src.core.fetch import WATCHER_USER_AGENT, HttpFetcher


class TestHttpFetcher:
    @pytest.mark.integration
    async def test_fetch_real_url(self):
        """Real-network smoke test: a live round-trip that MockTransport can't
        exercise. A 5xx or transport error is the host/network being down, not a
        fetcher bug — skip so third-party downtime doesn't surface as red (#213)."""
        fetcher = HttpFetcher()
        try:
            result = await fetcher.fetch("https://example.com")
        except httpx.TransportError as exc:
            pytest.skip(f"network unavailable: {exc!r}")
        finally:
            await fetcher.aclose()
        if result.status_code >= 500:
            pytest.skip(f"example.com unavailable (HTTP {result.status_code})")
        assert result.is_success
        assert len(result.content) > 0

    async def test_fetch_returns_co_core_result_shape(self):
        mock_response = httpx.Response(
            200,
            content=b"<html>test</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fetcher = HttpFetcher(client=client)
        result = await fetcher.fetch("https://example.com")
        assert result.is_success
        assert result.content == b"<html>test</html>"
        assert result.status_code == 200
        assert result.headers.get("content-type") == "text/html"
        assert result.duration_ms >= 0
        assert result.fetcher_used == "http"

    async def test_fetch_injects_watcher_user_agent(self):
        """Byte-continuity: the adapter must send watcher/0.1.0, not co-core-aio's
        default UA — otherwise the whole watch set re-fingerprints once (#236)."""
        captured = {}

        def handler(request):
            captured.update(dict(request.headers))
            return httpx.Response(200, content=b"ok", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = HttpFetcher(client=client)
        await fetcher.fetch("https://example.com")
        assert captured.get("user-agent") == WATCHER_USER_AGENT
        assert WATCHER_USER_AGENT == "watcher/0.1.0"

    async def test_fetch_merges_custom_headers_over_default_ua(self):
        captured = {}

        def handler(request):
            captured.update(dict(request.headers))
            return httpx.Response(200, content=b"ok", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fetcher = HttpFetcher(client=client)
        await fetcher.fetch("https://example.com", config={"headers": {"X-Custom": "test"}})
        assert captured.get("x-custom") == "test"
        assert captured.get("user-agent") == WATCHER_USER_AGENT


class TestHttpFetcherAclose:
    async def test_aclose_when_never_fetched_is_noop(self):
        fetcher = HttpFetcher()
        await fetcher.aclose()  # must not raise

    async def test_aclose_after_fetch_is_idempotent(self):
        mock_response = httpx.Response(
            200, content=b"ok", request=httpx.Request("GET", "https://example.com")
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: mock_response))
        fetcher = HttpFetcher(client=client)
        await fetcher.fetch("https://example.com")
        await fetcher.aclose()
        await fetcher.aclose()  # second call must not raise
