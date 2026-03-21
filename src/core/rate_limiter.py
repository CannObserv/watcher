"""Per-domain async rate limiter — coordinates concurrent access to domains."""

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from src.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MIN_INTERVAL = 1.0
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX_INTERVAL = 60.0


@dataclass
class DomainState:
    """Rate limiting state for a single domain."""

    semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(DEFAULT_MAX_CONCURRENT)
    )
    last_request_at: float = 0.0
    min_interval: float = DEFAULT_MIN_INTERVAL
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DomainRateLimiter:
    """Coordinate per-domain rate limiting for URL fetches."""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ):
        self._max_concurrent = max_concurrent
        self._default_min_interval = min_interval
        self._domains: dict[str, DomainState] = defaultdict(
            lambda: DomainState(
                semaphore=asyncio.Semaphore(self._max_concurrent),
                min_interval=self._default_min_interval,
            )
        )

    def extract_domain(self, url: str) -> str:
        """Extract hostname from a URL."""
        return urlparse(url).hostname or ""

    @asynccontextmanager
    async def acquire(self, url: str):
        """Async context manager: acquire rate-limited slot for a URL's domain."""
        domain = self.extract_domain(url)
        state = self._domains[domain]
        await state.semaphore.acquire()
        try:
            async with state.lock:
                now = time.monotonic()
                elapsed = now - state.last_request_at
                if elapsed < state.min_interval:
                    await asyncio.sleep(state.min_interval - elapsed)
                state.last_request_at = time.monotonic()
            yield
        finally:
            state.semaphore.release()

    def report_rate_limited(self, url: str) -> None:
        """Report a 429 response — increase the domain's min_interval via backoff."""
        domain = self.extract_domain(url)
        state = self._domains[domain]
        new_interval = max(state.min_interval * BACKOFF_MULTIPLIER, 2.0)
        state.min_interval = min(new_interval, BACKOFF_MAX_INTERVAL)
        logger.warning(
            "rate limited, increasing interval",
            extra={"domain": domain, "new_interval": state.min_interval},
        )
