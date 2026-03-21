# Phase 3: Fetching & Scheduling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the fetch-check-store pipeline: an HTTP fetcher, per-domain rate limiter, procrastinate task queue, a `check_watch` task that orchestrates fetch → extract → diff → store, and a scheduler that finds due watches and defers check jobs.

**Architecture:** Procrastinate (PostgreSQL-based async task queue) replaces arq/Redis from the original design. A periodic scheduler task runs every minute, queries for due watches, and defers `check_watch` jobs. Each `check_watch` job acquires a per-domain rate limit slot, fetches content via httpx, runs the extractor + differ pipeline from Phase 2, stores snapshots/chunks/changes, and defers its own next run based on `schedule_config`. FastAPI lifespan embeds the procrastinate worker in-process for now.

**Tech Stack:** procrastinate (task queue), psycopg[binary] (procrastinate connector), httpx (async HTTP), existing Phase 2 extractors/differ/models

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  core/
    fetchers/
      __init__.py          — create: re-export protocol and implementations
      base.py              — create: Fetcher protocol, FetchResult dataclass
      http.py              — create: httpx-based async HTTP fetcher
    rate_limiter.py        — create: per-domain async rate limiter
    scheduler.py           — create: find due watches, compute next check time
  workers/
    __init__.py            — create: procrastinate App setup
    tasks.py               — create: check_watch and schedule_tick tasks
  api/
    main.py                — modify: add procrastinate worker to FastAPI lifespan
tests/
  core/
    fetchers/
      __init__.py          — create: package
      test_http.py         — create: HTTP fetcher tests
    test_rate_limiter.py   — create: rate limiter tests
    test_scheduler.py      — create: scheduler logic tests
  workers/
    __init__.py            — create: package
    test_tasks.py          — create: check_watch pipeline tests
```

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add production dependencies**

Add to `pyproject.toml` `dependencies`:
```
"procrastinate[psycopg]>=3.0",
"httpx>=0.28.0",
```

Note: `httpx` is already a dev dependency. Move it to production dependencies (it's needed by the HTTP fetcher at runtime, not just tests).

- [ ] **Step 2: Install**

Run: `uv sync`
Expected: resolves and installs without errors

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "#2 chore: add procrastinate, httpx production dependencies"
```

---

## Task 2: Fetcher protocol and HTTP fetcher

**Files:**
- Create: `src/core/fetchers/__init__.py`
- Create: `src/core/fetchers/base.py`
- Create: `src/core/fetchers/http.py`
- Create: `tests/core/fetchers/__init__.py`
- Create: `tests/core/fetchers/test_http.py`

- [ ] **Step 1: Write failing tests for FetchResult and HttpFetcher**

Create `tests/core/fetchers/__init__.py` (empty).

Create `tests/core/fetchers/test_http.py`:

```python
"""Tests for HTTP fetcher."""

import httpx
import pytest

from src.core.fetchers.base import FetchResult
from src.core.fetchers.http import HttpFetcher


class TestFetchResult:
    def test_create_result(self):
        result = FetchResult(
            content=b"<html>hello</html>",
            status_code=200,
            headers={"content-type": "text/html"},
            duration_ms=150,
            fetcher_used="http",
        )
        assert result.content == b"<html>hello</html>"
        assert result.status_code == 200
        assert result.duration_ms == 150
        assert result.fetcher_used == "http"

    def test_is_success(self):
        result = FetchResult(
            content=b"ok", status_code=200, headers={},
            duration_ms=100, fetcher_used="http",
        )
        assert result.is_success is True

    def test_is_not_success(self):
        result = FetchResult(
            content=b"", status_code=404, headers={},
            duration_ms=100, fetcher_used="http",
        )
        assert result.is_success is False


class TestHttpFetcher:
    @pytest.mark.integration
    async def test_fetch_real_url(self):
        fetcher = HttpFetcher()
        result = await fetcher.fetch("https://httpbin.org/html")
        assert result.is_success
        assert len(result.content) > 0
        assert result.fetcher_used == "http"

    async def test_fetch_with_mock_client(self):
        mock_response = httpx.Response(
            200,
            content=b"<html>test</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: mock_response
        ))
        fetcher = HttpFetcher(client=mock_client)
        result = await fetcher.fetch("https://example.com")
        assert result.is_success
        assert result.content == b"<html>test</html>"
        assert result.fetcher_used == "http"

    async def test_fetch_timeout(self):
        async def slow_handler(request):
            import asyncio
            await asyncio.sleep(10)
            return httpx.Response(200)

        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler),
            timeout=httpx.Timeout(0.01),
        )
        fetcher = HttpFetcher(client=mock_client)
        with pytest.raises(httpx.TimeoutException):
            await fetcher.fetch("https://example.com")

    async def test_fetch_records_duration(self):
        mock_response = httpx.Response(
            200, content=b"ok",
            request=httpx.Request("GET", "https://example.com"),
        )
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: mock_response
        ))
        fetcher = HttpFetcher(client=mock_client)
        result = await fetcher.fetch("https://example.com")
        assert result.duration_ms >= 0

    async def test_fetch_passes_custom_headers(self):
        captured_headers = {}

        def capture_handler(request):
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, content=b"ok",
                                  request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(capture_handler))
        fetcher = HttpFetcher(client=mock_client)
        await fetcher.fetch("https://example.com", config={"headers": {"X-Custom": "test"}})
        assert captured_headers.get("x-custom") == "test"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/fetchers/test_http.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement fetcher protocol**

Create `src/core/fetchers/__init__.py`:

```python
"""Content fetchers — retrieve raw bytes from URLs."""

from src.core.fetchers.base import Fetcher, FetchResult
from src.core.fetchers.http import HttpFetcher

__all__ = ["Fetcher", "FetchResult", "HttpFetcher"]
```

Create `src/core/fetchers/base.py`:

```python
"""Fetcher protocol and shared data structures."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class FetchResult:
    """Result of fetching a URL."""

    content: bytes
    status_code: int
    headers: dict
    duration_ms: int
    fetcher_used: str

    @property
    def is_success(self) -> bool:
        """True if the HTTP status indicates success."""
        return 200 <= self.status_code < 400


class Fetcher(Protocol):
    """Protocol for URL fetchers."""

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch content from a URL."""
        ...
```

- [ ] **Step 4: Implement HTTP fetcher**

Create `src/core/fetchers/http.py`:

```python
"""HTTP fetcher — async content retrieval via httpx."""

import time

import httpx

from src.core.fetchers.base import FetchResult

DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "watcher/0.1.0 (+https://github.com/CannObserv/watcher)"


class HttpFetcher:
    """Fetch URL content using httpx async HTTP client."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch content from a URL.

        Config keys:
            headers: dict — additional HTTP headers
            timeout: float — request timeout in seconds (default: 30)
        """
        config = config or {}
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        headers.update(config.get("headers", {}))
        timeout = config.get("timeout", DEFAULT_TIMEOUT)

        client = self._client or httpx.AsyncClient(timeout=timeout)
        try:
            start = time.monotonic()
            response = await client.get(url, headers=headers, follow_redirects=True)
            duration_ms = int((time.monotonic() - start) * 1000)

            return FetchResult(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers),
                duration_ms=duration_ms,
                fetcher_used="http",
            )
        finally:
            if self._owns_client and not self._client:
                await client.aclose()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/fetchers/test_http.py -v`
Expected: unit tests pass (integration test skipped by default)

- [ ] **Step 6: Commit**

```bash
git add src/core/fetchers/ tests/core/fetchers/
git commit -m "#2 feat: add Fetcher protocol and httpx HTTP fetcher"
```

---

## Task 3: Per-domain rate limiter

**Files:**
- Create: `src/core/rate_limiter.py`
- Create: `tests/core/test_rate_limiter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_rate_limiter.py`:

```python
"""Tests for per-domain async rate limiter."""

import asyncio
import time
from urllib.parse import urlparse

import pytest

from src.core.rate_limiter import DomainRateLimiter


class TestDomainRateLimiter:
    def test_extract_domain(self):
        limiter = DomainRateLimiter()
        assert limiter.extract_domain("https://example.com/path") == "example.com"
        assert limiter.extract_domain("https://sub.example.com:8080/") == "sub.example.com"

    async def test_acquire_release(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        async with limiter.acquire("https://example.com/a"):
            pass  # should not block

    async def test_max_concurrent_enforced(self):
        limiter = DomainRateLimiter(max_concurrent=1, min_interval=0.0)
        acquired = []

        async def task(url, delay):
            async with limiter.acquire(url):
                acquired.append(time.monotonic())
                await asyncio.sleep(delay)

        # Two tasks for same domain, max_concurrent=1 — must serialize
        await asyncio.gather(
            task("https://example.com/a", 0.05),
            task("https://example.com/b", 0.05),
        )
        assert len(acquired) == 2
        # Second acquisition should happen after first releases (~0.05s later)
        assert acquired[1] - acquired[0] >= 0.04

    async def test_different_domains_independent(self):
        limiter = DomainRateLimiter(max_concurrent=1, min_interval=0.0)
        acquired = []

        async def task(url):
            async with limiter.acquire(url):
                acquired.append(time.monotonic())
                await asyncio.sleep(0.05)

        # Different domains should run concurrently
        await asyncio.gather(
            task("https://example.com/a"),
            task("https://other.com/b"),
        )
        assert len(acquired) == 2
        # Should start nearly simultaneously
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
        assert times[1] - times[0] >= 0.09  # min_interval enforced

    async def test_backoff_on_429(self):
        limiter = DomainRateLimiter(max_concurrent=2, min_interval=0.0)
        limiter.report_rate_limited("https://example.com/a")
        # After 429, the domain's min_interval should increase
        domain = limiter.extract_domain("https://example.com/a")
        state = limiter._domains[domain]
        assert state.min_interval > 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_rate_limiter.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement rate limiter**

Create `src/core/rate_limiter.py`:

```python
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
DEFAULT_MIN_INTERVAL = 1.0  # seconds between requests to same domain
BACKOFF_MULTIPLIER = 2.0
BACKOFF_MAX_INTERVAL = 60.0


@dataclass
class DomainState:
    """Rate limiting state for a single domain."""

    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(DEFAULT_MAX_CONCURRENT))
    last_request_at: float = 0.0
    min_interval: float = DEFAULT_MIN_INTERVAL
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DomainRateLimiter:
    """Coordinate per-domain rate limiting for URL fetches."""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._default_min_interval = min_interval
        self._domains: dict[str, DomainState] = defaultdict(
            lambda: DomainState(
                semaphore=asyncio.Semaphore(self._max_concurrent),
                min_interval=self._default_min_interval,
            )
        )

    def extract_domain(self, url: str) -> str:
        """Extract the domain (hostname) from a URL."""
        return urlparse(url).hostname or ""

    @asynccontextmanager
    async def acquire(self, url: str):
        """Acquire a rate limit slot for the domain of the given URL.

        Blocks until both: a semaphore slot is available AND min_interval
        has elapsed since the last request to this domain.
        """
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
        """Report that a 429 was received — increase backoff for this domain."""
        domain = self.extract_domain(url)
        state = self._domains[domain]
        new_interval = min(
            state.min_interval * BACKOFF_MULTIPLIER,
            BACKOFF_MAX_INTERVAL,
        )
        if new_interval == state.min_interval:
            new_interval = max(self._default_min_interval * BACKOFF_MULTIPLIER, 2.0)
        state.min_interval = new_interval
        logger.warning(
            "rate limited, increasing interval",
            extra={"domain": domain, "new_interval": new_interval},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_rate_limiter.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/rate_limiter.py tests/core/test_rate_limiter.py
git commit -m "#2 feat: add per-domain async rate limiter with 429 backoff"
```

---

## Task 4: Scheduler logic (find due watches)

**Files:**
- Create: `src/core/scheduler.py`
- Create: `tests/core/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_scheduler.py`:

```python
"""Tests for scheduler — compute next check time from schedule_config."""

from datetime import UTC, datetime, timedelta

from src.core.scheduler import compute_next_check, parse_interval


class TestParseInterval:
    def test_parse_seconds(self):
        assert parse_interval("30s") == timedelta(seconds=30)

    def test_parse_minutes(self):
        assert parse_interval("15m") == timedelta(minutes=15)

    def test_parse_hours(self):
        assert parse_interval("6h") == timedelta(hours=6)

    def test_parse_days(self):
        assert parse_interval("1d") == timedelta(days=1)

    def test_default_is_daily(self):
        assert parse_interval(None) == timedelta(days=1)

    def test_invalid_raises(self):
        import pytest
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("abc")


class TestComputeNextCheck:
    def test_no_previous_check_returns_now(self):
        now = datetime.now(UTC)
        result = compute_next_check(
            schedule_config={"interval": "1h"},
            last_checked_at=None,
            now=now,
        )
        assert result <= now

    def test_interval_from_last_check(self):
        now = datetime.now(UTC)
        last = now - timedelta(minutes=30)
        result = compute_next_check(
            schedule_config={"interval": "1h"},
            last_checked_at=last,
            now=now,
        )
        # Next check = last + 1h = now + 30m
        expected = last + timedelta(hours=1)
        assert result == expected

    def test_overdue_returns_now(self):
        now = datetime.now(UTC)
        last = now - timedelta(hours=2)
        result = compute_next_check(
            schedule_config={"interval": "1h"},
            last_checked_at=last,
            now=now,
        )
        # Overdue: last + 1h < now, so return now
        assert result <= now

    def test_default_interval_daily(self):
        now = datetime.now(UTC)
        last = now - timedelta(hours=12)
        result = compute_next_check(
            schedule_config={},
            last_checked_at=last,
            now=now,
        )
        expected = last + timedelta(days=1)
        assert result == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_scheduler.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement scheduler logic**

Create `src/core/scheduler.py`:

```python
"""Scheduler logic — compute when watches are due for checking."""

import re
from datetime import UTC, datetime, timedelta

DEFAULT_INTERVAL = timedelta(days=1)

INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$")
INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_interval(value: str | None) -> timedelta:
    """Parse a human-readable interval string to timedelta.

    Supported formats: '30s', '15m', '6h', '1d'.
    Returns DEFAULT_INTERVAL if value is None.
    """
    if value is None:
        return DEFAULT_INTERVAL

    match = INTERVAL_PATTERN.match(value)
    if not match:
        raise ValueError(f"Invalid interval: {value!r}. Use format like '30s', '15m', '6h', '1d'.")

    amount = int(match.group(1))
    unit = INTERVAL_UNITS[match.group(2)]
    return timedelta(**{unit: amount})


def compute_next_check(
    schedule_config: dict,
    last_checked_at: datetime | None,
    now: datetime | None = None,
) -> datetime:
    """Compute when a watch should next be checked.

    Returns a datetime. If the watch is overdue or never checked, returns now.
    """
    now = now or datetime.now(UTC)
    interval = parse_interval(schedule_config.get("interval"))

    if last_checked_at is None:
        return now

    next_due = last_checked_at + interval
    if next_due <= now:
        return now
    return next_due
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_scheduler.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/scheduler.py tests/core/test_scheduler.py
git commit -m "#2 feat: add scheduler logic (interval parsing, next check computation)"
```

---

## Task 5: Add `last_checked_at` to Watch model

**Files:**
- Modify: `src/core/models/watch.py`
- Modify: `tests/core/test_models.py`
- New: Alembic migration

The scheduler needs to know when each watch was last checked to compute next due time. Add a nullable `last_checked_at` column to the Watch model.

- [ ] **Step 1: Write failing test**

Add to `tests/core/test_models.py`:

```python
class TestWatchLastChecked:
    def test_last_checked_at_defaults_to_none(self):
        watch = Watch(
            name="New Watch",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        assert watch.last_checked_at is None
```

- [ ] **Step 2: Add column to Watch model**

Add to `src/core/models/watch.py`:

```python
from datetime import datetime
from sqlalchemy import DateTime

# In the Watch class, add:
last_checked_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True, default=None,
)
```

- [ ] **Step 3: Generate and apply migration**

```bash
export $(cat env | xargs)
uv run alembic revision --autogenerate -m "add last_checked_at to watches"
uv run alembic upgrade head
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/core/models/watch.py tests/core/test_models.py alembic/
git commit -m "#2 feat: add last_checked_at column to Watch model"
```

---

## Task 6: Procrastinate app setup

**Files:**
- Create: `src/workers/__init__.py`
- Modify: `pyproject.toml` (add `PROCRASTINATE_DATABASE_URL` to env docs)

- [ ] **Step 1: Create workers package with procrastinate App (lazy init)**

Create `src/workers/__init__.py`:

```python
"""Procrastinate task queue — app setup and worker configuration.

Uses lazy initialization to avoid import-time side effects.
Call get_app() to get the configured App instance.
"""

import os

import procrastinate

from src.core.logging import get_logger

logger = get_logger(__name__)

_app: procrastinate.App | None = None

# Blueprint for task registration — tasks register against this, not the App directly.
# This avoids circular imports since tasks.py can import bp without triggering App creation.
bp = procrastinate.Blueprint()


def _get_conninfo() -> str:
    """Get libpq-style connection string for procrastinate."""
    url = os.environ.get("PROCRASTINATE_DATABASE_URL")
    if url:
        return url
    sa_url = os.environ.get("DATABASE_URL", "")
    if sa_url.startswith("postgresql+asyncpg://"):
        return sa_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if sa_url.startswith("postgresql://"):
        return sa_url
    raise RuntimeError(
        "PROCRASTINATE_DATABASE_URL or DATABASE_URL environment variable is not set."
    )


def get_app() -> procrastinate.App:
    """Return the procrastinate App, creating it on first call."""
    global _app
    if _app is None:
        _app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=_get_conninfo()),
            import_paths=["src.workers.tasks"],
        )
        _app.add_tasks_from(bp, namespace="")
        logger.info("procrastinate app created")
    return _app


def reset_app() -> None:
    """Reset the App singleton. For testing only."""
    global _app
    _app = None


# CLI alias — procrastinate CLI resolves `--app=src.workers.app` to this module-level name.
# Lazy: triggers get_app() on first attribute access via __getattr__.
def __getattr__(name: str):
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

NOTE: Tasks register against `bp` (Blueprint). The App is only created when `get_app()` is called (at worker start, FastAPI lifespan, or CLI via `__getattr__`), not at import time. This means tests can import `src.workers.tasks` without needing DATABASE_URL.

- [ ] **Step 2: Apply procrastinate schema**

Run:
```bash
export $(cat env | xargs)
procrastinate --app=src.workers.app schema --apply
```

If `procrastinate` CLI doesn't find the app, use:
```bash
uv run procrastinate --app=src.workers.app schema --apply
```

This creates procrastinate's internal tables in PostgreSQL.

- [ ] **Step 3: Commit**

```bash
git add src/workers/__init__.py
git commit -m "#2 feat: add procrastinate App setup with PsycopgConnector"
```

---

## Task 7: check_watch task

**Files:**
- Create: `src/workers/tasks.py`
- Create: `tests/workers/__init__.py`
- Create: `tests/workers/test_tasks.py`

This is the core orchestration task. It ties together fetcher, extractor, differ, storage, and models.

- [ ] **Step 0: Add ChunkFingerprint to differ.py**

Add to `src/core/differ.py`:

```python
from typing import Protocol

class Diffable(Protocol):
    """Anything with index, label, content_hash, and simhash."""
    index: int
    label: str
    content_hash: str
    simhash: int

@dataclass
class ChunkFingerprint:
    """Lightweight DTO for chunk comparison — no text, no recomputation."""
    index: int
    label: str
    content_hash: str
    simhash: int
```

Update `diff_chunks` signature to accept `Diffable` instead of `Chunk`:

```python
def diff_chunks(previous: list, current: list) -> list[ChunkChange]:
```

(Use `list` — both `Chunk` and `ChunkFingerprint` satisfy the duck-typed access pattern. A `Protocol` type hint is cleaner but not required.)

Add test to `tests/core/test_differ.py`:

```python
from src.core.differ import ChunkFingerprint

class TestChunkFingerprint:
    def test_diff_with_fingerprints(self):
        prev = [ChunkFingerprint(index=0, label="P1", content_hash="aaa", simhash=100)]
        curr = [ChunkFingerprint(index=0, label="P1", content_hash="bbb", simhash=101)]
        result = diff_chunks(prev, curr)
        assert result[0].status == ChangeStatus.MODIFIED

    def test_mixed_unchanged(self):
        fp = ChunkFingerprint(index=0, label="P1", content_hash="same", simhash=42)
        result = diff_chunks([fp], [ChunkFingerprint(index=0, label="P1", content_hash="same", simhash=42)])
        assert result[0].status == ChangeStatus.UNCHANGED
```

Run existing differ tests to confirm backward compatibility: `uv run pytest tests/core/test_differ.py -v`

Commit: `git commit -m "#2 refactor: add ChunkFingerprint DTO, generalize diff_chunks"`

- [ ] **Step 1: Write failing tests for check_watch pipeline**

Create `tests/workers/__init__.py` (empty).

Create `tests/workers/test_tasks.py`:

```python
"""Tests for procrastinate tasks — check_watch pipeline."""

import hashlib

import pytest
from src.core.extractors.base import Chunk
from src.workers.tasks import _run_check_pipeline


class TestCheckPipeline:
    """Test the check_watch pipeline logic directly (no procrastinate queue)."""

    async def test_pipeline_creates_snapshot_on_first_check(self, db_session, tmp_path):
        from src.core.models.watch import ContentType, Watch
        from src.core.storage import LocalStorage

        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Hello world</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )

        assert result is not None
        assert result["snapshot_id"] is not None
        assert result["is_changed"] is True  # first check is always "new"
        assert result["chunk_count"] >= 1

    async def test_pipeline_detects_no_change(self, db_session, tmp_path):
        from src.core.models.watch import ContentType, Watch
        from src.core.storage import LocalStorage

        watch = Watch(
            name="Stable",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Same content</p></body></html>"

        # First check
        await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        # Second check — same content
        result = await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )
        assert result["is_changed"] is False

    async def test_pipeline_detects_change(self, db_session, tmp_path):
        from src.core.models.watch import ContentType, Watch
        from src.core.storage import LocalStorage

        watch = Watch(
            name="Changing",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)

        # First check
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>Version 1</p></body></html>",
            fetcher_used="http", fetch_duration_ms=100,
            storage=storage, session=db_session,
        )
        # Second check — different content
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>Version 2</p></body></html>",
            fetcher_used="http", fetch_duration_ms=100,
            storage=storage, session=db_session,
        )
        assert result["is_changed"] is True
        assert result["change_id"] is not None

    async def test_pipeline_stores_raw_content(self, db_session, tmp_path):
        from src.core.models.watch import ContentType, Watch
        from src.core.storage import LocalStorage

        watch = Watch(
            name="Storage Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Stored content</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch, raw_content=content, fetcher_used="http",
            fetch_duration_ms=100, storage=storage, session=db_session,
        )

        # Verify raw content was stored
        stored = storage.load(result["storage_path"])
        assert stored == content


pytestmark = pytest.mark.integration
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/workers/test_tasks.py -v -m integration`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement check_watch pipeline**

Create `src/workers/tasks.py`:

```python
"""Procrastinate tasks — check_watch pipeline and scheduler."""

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.differ import ChangeStatus, diff_chunks
from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import ExtractionResult
from src.core.logging import get_logger
from src.core.models.audit_log import AuditLog
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch
from src.core.storage import StorageBackend

logger = get_logger(__name__)

EXTRACTORS = {
    ContentType.HTML: HtmlExtractor(),
    ContentType.PDF: PdfExtractor(),
    ContentType.FILE: CsvExcelExtractor(),
}


def _get_extractor(content_type: ContentType):
    """Return the appropriate extractor for a content type."""
    return EXTRACTORS.get(content_type, HtmlExtractor())


async def _get_previous_snapshot(
    session: AsyncSession, watch_id: ULID
) -> Snapshot | None:
    """Get the most recent snapshot for a watch."""
    stmt = (
        select(Snapshot)
        .where(Snapshot.watch_id == watch_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_snapshot_chunks(
    session: AsyncSession, snapshot_id: ULID
) -> list[SnapshotChunk]:
    """Load all chunks for a snapshot."""
    stmt = (
        select(SnapshotChunk)
        .where(SnapshotChunk.snapshot_id == snapshot_id)
        .order_by(SnapshotChunk.chunk_index)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _run_check_pipeline(
    watch: Watch,
    raw_content: bytes,
    fetcher_used: str,
    fetch_duration_ms: int,
    storage: StorageBackend,
    session: AsyncSession,
) -> dict:
    """Core check pipeline: extract → compare → store.

    Returns a dict with snapshot_id, is_changed, change_id, chunk_count, storage_path.
    """
    from src.core.extractors.base import Chunk as ExtractorChunk
    from src.core.simhash import simhash as compute_simhash

    content_hash = hashlib.sha256(raw_content).hexdigest()
    doc_simhash = compute_simhash(raw_content.decode("utf-8", errors="replace"))

    # Check if content is identical to previous snapshot (fast path)
    prev_snapshot = await _get_previous_snapshot(session, watch.id)
    if prev_snapshot and prev_snapshot.content_hash == content_hash:
        logger.info("no change (identical hash)", extra={"watch_id": str(watch.id)})
        session.add(AuditLog(
            event_type="check.completed",
            watch_id=watch.id,
            payload={"changed": False, "fetcher": fetcher_used},
        ))
        await session.flush()
        return {
            "snapshot_id": None,
            "is_changed": False,
            "change_id": None,
            "chunk_count": 0,
            "storage_path": None,
        }

    # Extract content into chunks
    extractor = _get_extractor(watch.content_type)
    config = dict(watch.fetch_config) if watch.fetch_config else {}
    if watch.content_type == ContentType.FILE:
        config.setdefault("content_type", "csv")
    extraction = extractor.extract(raw_content, config=config)

    # Store raw content and extracted text
    from src.core.models.base import generate_ulid

    snapshot_id = generate_ulid()
    ext = {"html": "html", "pdf": "pdf", "file": "csv"}.get(watch.content_type.value, "bin")
    storage_path = storage.snapshot_path(str(watch.id), str(snapshot_id), ext)
    text_path = storage.snapshot_path(str(watch.id), str(snapshot_id), "txt")

    storage.save(storage_path, raw_content)
    full_text = "\n".join(c.text for c in extraction.chunks)
    storage.save(text_path, full_text.encode("utf-8"))

    # Create snapshot record
    snapshot = Snapshot(
        id=snapshot_id,
        watch_id=watch.id,
        content_hash=content_hash,
        simhash=doc_simhash,
        storage_path=storage_path,
        text_path=text_path,
        storage_backend="local",
        chunk_count=len(extraction.chunks),
        text_bytes=len(full_text.encode("utf-8")),
        fetch_duration_ms=fetch_duration_ms,
        fetcher_used=fetcher_used,
    )
    session.add(snapshot)

    # Store chunks
    for chunk in extraction.chunks:
        session.add(SnapshotChunk(
            snapshot_id=snapshot_id,
            chunk_index=chunk.index,
            chunk_type=chunk.chunk_type,
            chunk_label=chunk.label,
            content_hash=chunk.content_hash,
            simhash=chunk.simhash,
            char_count=chunk.char_count,
            excerpt=chunk.excerpt,
        ))

    # Diff against previous snapshot
    change_id = None
    if prev_snapshot:
        prev_chunks_db = await _get_snapshot_chunks(session, prev_snapshot.id)
        # Use ChunkFingerprint for comparison — avoids reconstructing full Chunk objects
        from src.core.differ import ChunkFingerprint

        prev_fingerprints = [
            ChunkFingerprint(
                index=c.chunk_index,
                label=c.chunk_label,
                content_hash=c.content_hash,
                simhash=c.simhash,
            )
            for c in prev_chunks_db
        ]
        curr_fingerprints = [
            ChunkFingerprint(
                index=c.index,
                label=c.label,
                content_hash=c.content_hash,
                simhash=c.simhash,
            )
            for c in extraction.chunks
        ]

        changes = diff_chunks(prev_fingerprints, curr_fingerprints)
        has_changes = any(c.status != ChangeStatus.UNCHANGED for c in changes)

        if has_changes:
            change_metadata = {
                "added": [{"index": c.chunk_index, "label": c.chunk_label}
                          for c in changes if c.status == ChangeStatus.ADDED],
                "removed": [{"index": c.chunk_index, "label": c.chunk_label}
                            for c in changes if c.status == ChangeStatus.REMOVED],
                "modified": [{"index": c.chunk_index, "label": c.chunk_label,
                              "similarity": c.similarity}
                             for c in changes if c.status == ChangeStatus.MODIFIED],
            }
            change = Change(
                watch_id=watch.id,
                previous_snapshot_id=prev_snapshot.id,
                current_snapshot_id=snapshot_id,
                change_metadata=change_metadata,
            )
            session.add(change)
            await session.flush()
            change_id = change.id
    else:
        has_changes = True  # First snapshot is always "new"

    session.add(AuditLog(
        event_type="check.completed",
        watch_id=watch.id,
        payload={
            "changed": has_changes,
            "snapshot_id": str(snapshot_id),
            "fetcher": fetcher_used,
            "chunk_count": len(extraction.chunks),
        },
    ))
    await session.flush()

    return {
        "snapshot_id": str(snapshot_id),
        "is_changed": has_changes,
        "change_id": str(change_id) if change_id else None,
        "chunk_count": len(extraction.chunks),
        "storage_path": storage_path,
    }
```

NOTE: The actual `@app.task` decorated `check_watch` function that wraps this pipeline with fetching + rate limiting + session management will be added in Task 7 (FastAPI integration), once we have the full wiring in place. For now, the pipeline is tested directly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/workers/test_tasks.py -v -m integration`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/workers/tasks.py tests/workers/
git commit -m "#2 feat: add check_watch pipeline (extract, diff, store)"
```

---

## Task 8: FastAPI lifespan + procrastinate worker integration

**Files:**
- Modify: `src/api/main.py`
- Modify: `src/workers/tasks.py` (add `@app.task` wrappers and `schedule_tick`)
- Modify: `src/workers/__init__.py` (ensure app can be imported cleanly)

- [ ] **Step 1: Add procrastinate task wrappers to `src/workers/tasks.py`**

Add to `src/workers/tasks.py`, using the Blueprint from `src/workers`:

```python
import procrastinate
from src.workers import bp, get_app
from src.core.fetchers.http import HttpFetcher
from src.core.rate_limiter import DomainRateLimiter
from src.core.scheduler import compute_next_check, parse_interval
from src.core.database import get_engine, get_session_factory

# Module-level shared resources (created once per worker process)
_fetcher = HttpFetcher()
_rate_limiter = DomainRateLimiter()


@bp.task(
    name="check_watch",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={ConnectionError, TimeoutError},
    ),
)
async def check_watch(watch_id: str) -> dict:
    """Fetch and check a single watch for changes."""
    from pathlib import Path
    from ulid import ULID
    from src.core.storage import LocalStorage

    async with get_session_factory()() as session:
        watch = await session.get(Watch, ULID.from_str(watch_id))
        if not watch or not watch.is_active:
            logger.warning("watch not found or inactive", extra={"watch_id": watch_id})
            return {"skipped": True}

        # Fetch content with rate limiting
        async with _rate_limiter.acquire(watch.url):
            fetch_result = await _fetcher.fetch(watch.url, config=watch.fetch_config)

        if fetch_result.status_code == 429:
            _rate_limiter.report_rate_limited(watch.url)
            raise ConnectionError(f"Rate limited by {watch.url}")

        if not fetch_result.is_success:
            logger.warning("fetch failed", extra={
                "watch_id": watch_id, "status": fetch_result.status_code,
            })
            return {"error": f"HTTP {fetch_result.status_code}"}

        # Run pipeline
        storage = LocalStorage(base_dir=Path("data"))
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=fetch_result.content,
            fetcher_used=fetch_result.fetcher_used,
            fetch_duration_ms=fetch_result.duration_ms,
            storage=storage,
            session=session,
        )

        # Update last_checked_at and read schedule_config before session closes
        from datetime import UTC, datetime
        watch.last_checked_at = datetime.now(UTC)
        interval_str = watch.schedule_config.get("interval")
        await session.commit()

    # Defer next check (outside session — interval_str captured above)
    interval = parse_interval(interval_str)
    await check_watch.configure(
        schedule_in={"seconds": int(interval.total_seconds())},
    ).defer_async(watch_id=watch_id)

    return result


@bp.periodic(cron="* * * * *")  # every minute
@bp.task(name="schedule_tick", queue="default")
async def schedule_tick(timestamp: int) -> None:
    """Find active watches due for checking and defer check_watch jobs."""
    from datetime import UTC, datetime
    from sqlalchemy import select, or_

    now = datetime.now(UTC)

    async with get_session_factory()() as session:
        # Find watches that are:
        # 1. Active
        # 2. Never checked (last_checked_at IS NULL), OR
        # 3. Overdue (last_checked_at + interval < now)
        stmt = select(Watch).where(
            Watch.is_active == True,  # noqa: E712
            or_(
                Watch.last_checked_at.is_(None),
                Watch.last_checked_at < now,  # simplified: scheduler checks interval
            ),
        )
        result = await session.execute(stmt)
        watches = result.scalars().all()

    for watch in watches:
        next_due = compute_next_check(
            schedule_config=watch.schedule_config,
            last_checked_at=watch.last_checked_at,
            now=now,
        )
        if next_due <= now:
            logger.info("deferring check", extra={"watch_id": str(watch.id)})
            await check_watch.configure().defer_async(watch_id=str(watch.id))
```

- [ ] **Step 2: Update FastAPI lifespan**

Update `src/api/main.py` to embed the procrastinate worker:

```python
"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes.watches import router as watches_router
from src.core.logging import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start procrastinate worker alongside FastAPI."""
    from src.workers import get_app

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(
        proc_app.run_worker_async(install_signal_handlers=False)
    )
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await proc_app.close_async()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)
app.include_router(watches_router)
```

- [ ] **Step 3: Test manually**

```bash
export $(cat env | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

Verify in logs that the procrastinate worker starts alongside FastAPI.

- [ ] **Step 4: Commit**

```bash
git add src/api/main.py src/workers/
git commit -m "#2 feat: embed procrastinate worker in FastAPI lifespan"
```

---

## Task 9: Update documentation

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/COMMANDS.md`

- [ ] **Step 1: Update AGENTS.md**

Add to project layout:
```
src/core/fetchers/   — URL fetchers (HTTP, browser, WebRecorder)
src/core/rate_limiter.py — Per-domain async rate limiting
src/core/scheduler.py    — Watch scheduling logic
src/workers/             — Procrastinate task queue (check_watch, schedule_tick)
```

Add to Secrets section:
```
- `PROCRASTINATE_DATABASE_URL` — (optional) libpq-style DSN for procrastinate; falls back to DATABASE_URL with driver prefix stripped
```

- [ ] **Step 2: Update COMMANDS.md**

Add procrastinate commands:
```bash
# Apply procrastinate schema (first time)
export $(cat env | xargs)
uv run procrastinate --app=src.workers.app schema --apply

# Run worker standalone (alternative to embedded mode)
uv run procrastinate --app=src.workers.app worker
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -v` and `uv run pytest -m integration -v`
Expected: all tests pass

- [ ] **Step 4: Run linter**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/COMMANDS.md
git commit -m "#2 docs: add procrastinate, fetcher, rate limiter, scheduler documentation"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Dependencies (procrastinate, httpx) | — |
| 2 | Fetcher protocol + HttpFetcher | ~5 unit + 1 integration |
| 3 | Per-domain rate limiter | ~6 unit |
| 4 | Scheduler logic (interval parsing, due computation) | ~7 unit |
| 5 | Add `last_checked_at` to Watch model + migration | ~1 unit |
| 6 | Procrastinate App setup (lazy init) + schema | manual verify |
| 7 | check_watch pipeline + ChunkFingerprint DTO | ~4 integration |
| 8 | FastAPI lifespan + task wrappers (check_watch, schedule_tick) | manual verify |
| 9 | Documentation | — |

Total: ~24 new automated tests
