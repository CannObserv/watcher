"""URL probe — resolve effective URL and domain by following redirects."""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from src.core.fetch_commands import WATCHER_USER_AGENT
from src.core.logging import get_logger

logger = get_logger(__name__)

PROBE_TIMEOUT = 15.0
# Derived so the two never drift (CR-26). Only ``WATCHER_USER_AGENT`` is
# fingerprint-critical — probes produce no revisions — but a reader finding two
# disagreeing version strings can't tell which one matters.
PROBE_USER_AGENT = f"{WATCHER_USER_AGENT} (probe)"


@dataclass(frozen=True)
class ProbeResult:
    """Result of probing a URL for redirect resolution."""

    effective_url: str
    effective_domain: str
    redirect_chain: list[str]
    status_code: int
    content_type: str | None


async def probe_url(url: str) -> ProbeResult:
    """Probe a URL by following redirects; return effective URL and domain.

    Uses HEAD to minimise bandwidth. Raises httpx errors on connection failure.

    Args:
        url: The URL to probe (may redirect).

    Returns:
        ProbeResult with effective_url, effective_domain, redirect_chain,
        status_code, and content_type.

    Raises:
        httpx.HTTPError: On connection or timeout failure.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.head(
            url,
            headers={"user-agent": PROBE_USER_AGENT},
            timeout=PROBE_TIMEOUT,
        )

    chain = [str(r.url) for r in response.history] + [str(response.url)]
    effective_url = str(response.url)
    effective_domain = urlparse(effective_url).hostname or ""
    content_type = response.headers.get("content-type")

    logger.info(
        "probe complete",
        extra={
            "original_url": url,
            "effective_url": effective_url,
            "redirects": len(response.history),
            "status_code": response.status_code,
        },
    )

    return ProbeResult(
        effective_url=effective_url,
        effective_domain=effective_domain,
        redirect_chain=chain,
        status_code=response.status_code,
        content_type=content_type,
    )
