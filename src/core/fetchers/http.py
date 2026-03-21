"""HTTP fetcher using httpx async client."""

import time

import httpx

from src.core.fetchers.base import FetchResult

DEFAULT_USER_AGENT = "watcher/0.1.0"
DEFAULT_TIMEOUT = 30.0


class HttpFetcher:
    """Fetches URLs over HTTP/HTTPS using httpx."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch content from a URL.

        Args:
            url: Target URL.
            config: Optional dict with 'headers' (dict) and 'timeout' (float).

        Returns:
            FetchResult with response data and timing.
        """
        config = config or {}
        timeout = config.get("timeout", DEFAULT_TIMEOUT)
        headers = {
            "user-agent": DEFAULT_USER_AGENT,
            **config.get("headers", {}),
        }

        # Reuse persistent client for connection pooling; create on first call
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True)

        start = time.monotonic()
        response = await self._client.get(
            url, headers=headers, timeout=timeout, follow_redirects=True
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        return FetchResult(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
            duration_ms=duration_ms,
            fetcher_used="http",
        )
