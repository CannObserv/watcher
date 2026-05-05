"""Fetch a URL and return rendered body + headers for InfoSpec authoring.

v1 supports HTTP-only fetches via ``HttpFetcher``. Playwright-based rendering
is wired in once #3 lands; until then ``render=True`` raises NotImplementedError.
"""

from dataclasses import dataclass
from typing import Protocol

from src.core.fetchers.base import FetchResult

# Body payloads larger than this cap get truncated in the response. The
# extractor still sees the full bytes server-side; the truncation only bounds
# the JSON returned to the caller.
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MiB


class HttpFetcherProtocol(Protocol):
    """Minimal interface required by ``fetch_and_render`` — for type checking + test stubs."""

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult: ...


@dataclass(frozen=True)
class FetchAndRenderResult:
    """Materialised fetch outcome for the route's response_model."""

    url: str
    status_code: int
    headers: dict[str, str]
    body: str
    body_bytes_total: int
    truncated: bool
    screenshot_url: str | None


async def fetch_and_render(
    fetcher: HttpFetcherProtocol,
    url: str,
    *,
    render: bool = False,
) -> FetchAndRenderResult:
    """Fetch ``url`` and return body + headers for downstream authoring tools.

    ``render=True`` is reserved for the Playwright fetcher (#3) and raises
    ``NotImplementedError`` until that path is wired. Body bytes are truncated
    in the response when they exceed ``MAX_BODY_BYTES``.
    """
    if render:
        raise NotImplementedError("Playwright fetcher not yet integrated (#3)")

    result = await fetcher.fetch(url)
    total_bytes = len(result.content)
    truncated = total_bytes > MAX_BODY_BYTES
    payload = result.content[:MAX_BODY_BYTES] if truncated else result.content
    body_str = payload.decode("utf-8", errors="replace")

    return FetchAndRenderResult(
        url=url,
        status_code=result.status_code,
        headers=result.headers,
        body=body_str,
        body_bytes_total=total_bytes,
        truncated=truncated,
        screenshot_url=None,
    )
