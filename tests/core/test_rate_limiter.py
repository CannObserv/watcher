"""Tests for per-domain async rate limiter."""

import asyncio
import time

from src.core.rate_limiter import DomainRateLimiter


class TestDomainRateLimiter:
    def test_extract_domain(self):
        limiter = DomainRateLimiter()
        assert limiter.extract_domain("https://example.com/path") == "example.com"
        assert limiter.extract_domain("https://sub.example.com:8080/") == "sub.example.com"

    async def test_acquire_release(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        async with limiter.acquire("https://example.com/a"):
            pass

    async def test_max_concurrent_enforced(self):
        limiter = DomainRateLimiter(max_concurrent=1, min_interval=0.0)
        acquired = []

        async def task(url, delay):
            async with limiter.acquire(url):
                acquired.append(time.monotonic())
                await asyncio.sleep(delay)

        await asyncio.gather(
            task("https://example.com/a", 0.05),
            task("https://example.com/b", 0.05),
        )
        assert len(acquired) == 2
        assert acquired[1] - acquired[0] >= 0.04

    async def test_different_domains_independent(self):
        limiter = DomainRateLimiter(max_concurrent=1, min_interval=0.0)
        acquired = []

        async def task(url):
            async with limiter.acquire(url):
                acquired.append(time.monotonic())
                await asyncio.sleep(0.05)

        await asyncio.gather(
            task("https://example.com/a"),
            task("https://other.com/b"),
        )
        assert len(acquired) == 2
        assert abs(acquired[1] - acquired[0]) < 0.03

    async def test_min_interval_enforced(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.1)
        times = []

        async def task(url):
            async with limiter.acquire(url):
                times.append(time.monotonic())

        await task("https://example.com/a")
        await task("https://example.com/b")
        assert len(times) == 2
        assert times[1] - times[0] >= 0.09

    async def test_backoff_on_429(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        limiter.report_rate_limited("https://example.com/a")
        domain = limiter.extract_domain("https://example.com/a")
        state = limiter._domains[domain]
        assert state.min_interval > 0.0

    def test_get_domain_states_empty(self):
        limiter = DomainRateLimiter()
        assert limiter.get_domain_states() == []

    def test_get_domain_states_reports_backoff(self):
        limiter = DomainRateLimiter(min_interval=1.0)
        # Trigger domain creation
        _ = limiter._domains["example.com"]
        limiter.report_rate_limited("https://example.com/a")
        states = limiter.get_domain_states()
        assert len(states) == 1
        assert states[0]["name"] == "example.com"
        assert states[0]["in_backoff"] is True
        assert states[0]["interval"] > 1.0
