"""Watcher fetch adapter over co-core's async fetch effect (#236).

Not a mirror — a thin integration seam. co-core owns the fetch logic
(``co_core_aio.fetch.AsyncFetchDriver``); this adapter drives it with watcher's
own User-Agent and presents the ``.fetch(url, config) -> FetchResult`` interface
that ``ServiceRegistry`` and the worker pipeline expect.

Setting the UA explicitly is load-bearing for change detection: co-core-aio's
default UA is ``co-core-aio``; watcher's fingerprints are UA-sensitive, so we pin
``watcher/0.1.0`` to preserve byte-continuity across the adoption cutover.
"""

from typing import Protocol

import httpx
from co_core.effects.fetch import FetchContent, FetchResult
from co_core_aio.fetch import AsyncFetchDriver

WATCHER_USER_AGENT = "watcher/0.1.0"
DEFAULT_TIMEOUT = 30.0


class Fetcher(Protocol):
    """Protocol for URL fetchers (the ServiceRegistry injection seam)."""

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch content from a URL."""
        ...


class HttpFetcher:
    """Fetches URLs via co-core's ``AsyncFetchDriver``, pinning watcher's UA.

    ``client`` is a test seam: pass an ``httpx.AsyncClient`` (e.g. with a
    ``MockTransport``) to intercept the round-trip. In production the driver is
    built lazily on first fetch and owns its own client, closed by ``aclose``.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._driver: AsyncFetchDriver | None = None

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch ``url``, injecting watcher's UA (overridable per-call).

        Args:
            url: Target URL.
            config: Optional dict with 'headers' (dict) and 'timeout' (float).

        Returns:
            co-core ``FetchResult`` (non-2xx captured, not raised).
        """
        config = config or {}
        headers = {
            "user-agent": WATCHER_USER_AGENT,
            **(config.get("headers") or {}),
        }
        effect = FetchContent(
            url=url,
            headers=headers,
            timeout=config.get("timeout", DEFAULT_TIMEOUT),
        )
        if self._driver is None:
            self._driver = (
                AsyncFetchDriver(client=self._client)
                if self._client is not None
                else AsyncFetchDriver()
            )
        return await self._driver.execute(effect)

    async def aclose(self) -> None:
        """Close the underlying driver if one was built. Idempotent.

        Safe before any fetch (``_driver`` is None) or after a prior close.
        The driver only closes a client it owns, so an injected test client is
        left for the test to manage.
        """
        if self._driver is not None:
            await self._driver.aclose()
            self._driver = None
