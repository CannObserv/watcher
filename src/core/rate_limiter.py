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
    current_interval: float = DEFAULT_MIN_INTERVAL
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
                current_interval=self._default_min_interval,
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
                if elapsed < state.current_interval:
                    await asyncio.sleep(state.current_interval - elapsed)
                state.last_request_at = time.monotonic()
            yield
        finally:
            state.semaphore.release()

    def get_domain_states(self) -> list[dict]:
        """Return current state of all tracked domains for monitoring.

        Returns list of dicts with 'name', 'min_interval', 'current_interval',
        and 'in_backoff' keys.
        """
        return sorted(
            [
                {
                    "name": domain,
                    "min_interval": state.min_interval,
                    "current_interval": state.current_interval,
                    "in_backoff": state.current_interval > state.min_interval,
                }
                for domain, state in self._domains.items()
            ],
            key=lambda d: d["name"],
        )

    def report_rate_limited(self, url: str) -> None:
        """Report a 429 response — increase the domain's current_interval via backoff."""
        domain = self.extract_domain(url)
        state = self._domains[domain]
        new_interval = max(state.current_interval * BACKOFF_MULTIPLIER, 2.0)
        state.current_interval = min(new_interval, BACKOFF_MAX_INTERVAL)
        logger.warning(
            "rate limited, increasing interval",
            extra={"domain": domain, "new_interval": state.current_interval},
        )

    def configure_domain(
        self,
        name: str,
        max_concurrency: int,
        min_interval: float,
        current_interval: float,
    ) -> None:
        """Hydrate in-memory state from a persisted Domain record.

        Loads both min_interval (the operator-configured floor) and
        current_interval (the effective rate, which may be elevated by backoff).
        Backoff state survives restarts via current_interval.
        """
        self._domains[name] = DomainState(
            semaphore=asyncio.Semaphore(max_concurrency),
            min_interval=min_interval,
            current_interval=current_interval,
        )

    def reset_domain_interval(self, domain: str, min_interval: float) -> None:
        """Reset a domain's current_interval to min_interval (clear backoff)."""
        if domain in self._domains:
            self._domains[domain].current_interval = min_interval

    @asynccontextmanager
    async def acquire_for_domain(self, domain: str):
        """Acquire rate-limited slot using a known domain name.

        Prefer over acquire(url) when effective_domain is already resolved.
        Unknown domains are auto-initialised with global defaults via defaultdict.
        """
        state = self._domains[domain]
        await state.semaphore.acquire()
        try:
            async with state.lock:
                now = time.monotonic()
                elapsed = now - state.last_request_at
                if elapsed < state.current_interval:
                    await asyncio.sleep(state.current_interval - elapsed)
                state.last_request_at = time.monotonic()
            yield
        finally:
            state.semaphore.release()

    def report_rate_limited_for_domain(self, domain: str) -> float:
        """Report a 429 for a known domain name; return the new interval.

        Use instead of report_rate_limited(url) when effective_domain is known.
        """
        state = self._domains[domain]
        new_interval = max(state.current_interval * BACKOFF_MULTIPLIER, 2.0)
        state.current_interval = min(new_interval, BACKOFF_MAX_INTERVAL)
        logger.warning(
            "rate limited, increasing interval",
            extra={"domain": domain, "new_interval": state.current_interval},
        )
        return state.current_interval


_rate_limiter: DomainRateLimiter | None = None


def get_rate_limiter() -> DomainRateLimiter:
    """Return the shared DomainRateLimiter, creating it on first call.

    Both the API app (for startup hydration) and workers (for fetch rate limiting)
    must import this function to share the same in-memory state.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = DomainRateLimiter()
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the shared rate limiter singleton. For testing only."""
    global _rate_limiter
    _rate_limiter = None
