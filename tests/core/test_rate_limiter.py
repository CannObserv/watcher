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


class TestConfigureDomain:
    def test_configure_domain_stores_current_interval_as_effective_rate(self):
        """configure_domain stores current_interval as the effective in-memory rate.

        DomainState only has min_interval — it is the effective rate used during
        acquire. configure_domain loads current_interval here so that backoff
        state persists across restarts. The operator min_interval floor is
        DB-only; in-memory state only tracks the current effective rate.
        """
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=1,
            current_interval=5.0,
        )
        state = limiter._domains["example.com"]
        assert state.min_interval == 5.0  # current_interval becomes the effective rate

    def test_configure_domain_sets_concurrency(self):
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=3,
            current_interval=1.0,
        )
        state = limiter._domains["example.com"]
        assert state.semaphore._value == 3

    async def test_acquire_for_domain_works(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        async with limiter.acquire_for_domain("example.com"):
            pass  # should not raise

    async def test_acquire_for_domain_uses_domain_config(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        limiter.configure_domain(
            name="example.com",
            max_concurrency=1,
            current_interval=0.1,
        )
        times = []

        async def task():
            async with limiter.acquire_for_domain("example.com"):
                times.append(asyncio.get_event_loop().time())

        await task()
        await task()
        assert times[1] - times[0] >= 0.09

    def test_report_rate_limited_for_domain_increases_interval(self):
        limiter = DomainRateLimiter(min_interval=1.0)
        limiter.report_rate_limited_for_domain("example.com")
        state = limiter._domains["example.com"]
        assert state.min_interval > 1.0

    def test_report_rate_limited_for_domain_returns_new_interval(self):
        limiter = DomainRateLimiter(min_interval=1.0)
        new_interval = limiter.report_rate_limited_for_domain("example.com")
        assert new_interval == limiter._domains["example.com"].min_interval


class TestGetRateLimiter:
    def test_get_rate_limiter_returns_same_instance(self):
        from src.core.rate_limiter import get_rate_limiter, reset_rate_limiter

        reset_rate_limiter()
        a = get_rate_limiter()
        b = get_rate_limiter()
        assert a is b
        reset_rate_limiter()  # clean up
