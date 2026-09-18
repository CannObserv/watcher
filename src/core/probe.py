"""URL probe — resolve effective URL and domain by following redirects."""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from src.core.egress import GuardedTransport, Resolver, resolve_addresses
from src.core.fetch_commands import WATCHER_USER_AGENT
from src.core.logging import get_logger

logger = get_logger(__name__)

PROBE_TIMEOUT = 15.0
# Derived so the two never drift (CR-26). Only ``WATCHER_USER_AGENT`` is
# fingerprint-critical — probes produce no revisions — but a reader finding two
# disagreeing version strings can't tell which one matters.
PROBE_USER_AGENT = f"{WATCHER_USER_AGENT} (probe)"


def build_probe_client(
    inner: httpx.AsyncBaseTransport | None = None,
    *,
    resolve: Resolver = resolve_addresses,
) -> httpx.AsyncClient:
    """The client every probe goes out on: redirects followed, destinations guarded.

    The guard is a transport rather than a check on the submitted URL because
    ``follow_redirects=True`` means the submitted URL does not decide the
    destination — see :mod:`src.core.egress` (#305). ``inner`` and ``resolve``
    are the seams a test replaces to assert a refusal happened before anything
    left the host.
    """
    return httpx.AsyncClient(
        transport=GuardedTransport(inner or httpx.AsyncHTTPTransport(), resolve=resolve),
        follow_redirects=True,
    )


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
        httpx.HTTPError: On connection, resolution or timeout failure.
        DestinationRefused: If any hop resolves into a range this host does not
            probe — loopback, RFC 1918, link-local, ULA or the tailnet (#305).
    """
    async with build_probe_client() as client:
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
