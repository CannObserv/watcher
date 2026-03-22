# Domains Table & Rate Limiter Persistence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `domains` table for persistent per-domain rate limiter configuration; resolve effective URLs at watch creation time via a probe step; expose domain config via a CRUD API.

**Architecture:** A new `Domain` model stores operator-configured rate limit floors and backoff state. Watch creation probes the URL via `httpx`, resolves `effective_url`/`effective_domain`, and upserts a `Domain` row with defaults. The rate limiter is hydrated from the DB at startup; backoff state is persisted back on 429 events in the worker. Domain config is exposed via `GET/PATCH/DELETE /api/domains[/{name}]`; `PATCH` acts as upsert. A public `POST /api/probe` endpoint is also added.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), PostgreSQL, Alembic, httpx, pytest, ulid-py

**Design doc:** `docs/plans/2026-03-22-domains-rate-limiter-persistence.md`

---

## File Map

### New files
| File | Responsibility |
|---|---|
| `src/core/models/domain.py` | `Domain` SQLAlchemy model |
| `src/core/probe.py` | `ProbeResult` dataclass + `probe_url()` async function |
| `src/api/schemas/domain.py` | `DomainPatch`, `DomainResponse` Pydantic schemas |
| `src/api/routes/domains.py` | `GET/PATCH/DELETE /api/domains[/{name}]` |
| `src/api/routes/probe.py` | `POST /api/probe` |
| `alembic/versions/XXXX_add_domains.py` | DB migration: domains table + watch columns |
| `tests/core/test_probe.py` | Unit tests for probe logic |
| `tests/core/test_hydrate.py` | Unit tests for startup hydration |
| `tests/api/test_domains.py` | Integration tests for domain endpoints |
| `tests/api/test_probe.py` | Integration tests for probe endpoint |
| `tests/workers/__init__.py` | (empty) |
| `tests/workers/test_tasks.py` | Unit tests for worker helpers |

### Modified files
| File | Change |
|---|---|
| `src/core/models/__init__.py` | Export `Domain` |
| `src/core/models/watch.py` | Add `effective_url`, `effective_domain` columns |
| `src/core/rate_limiter.py` | Add `get_rate_limiter()` singleton, `configure_domain()`, `acquire_for_domain()`, `report_rate_limited_for_domain()` |
| `src/api/schemas/watch.py` | Add fields to `WatchResponse`; remove `url` from `WatchUpdate` |
| `src/api/routes/watches.py` | Probe on create; upsert domain |
| `src/api/dependencies.py` | Add `get_probe_fn` dependency |
| `src/api/main.py` | Register new routers; startup hydration |
| `src/workers/tasks.py` | Use `get_rate_limiter()` from `rate_limiter`; use `effective_domain`; persist backoff to DB on 429 |
| `tests/conftest.py` | Override `get_probe_fn` with mock in `client` fixture |
| `tests/core/test_models.py` | Add `TestDomainModel` |
| `tests/core/test_rate_limiter.py` | Test `configure_domain`, `acquire_for_domain`, `report_rate_limited_for_domain` |

---

## Task 1: Domain model + Watch columns + migration

**Files:**
- Create: `src/core/models/domain.py`
- Modify: `src/core/models/__init__.py`
- Modify: `src/core/models/watch.py`
- Modify: `tests/core/test_models.py`
- Create: migration via `alembic revision --autogenerate`

---

- [ ] **Step 1.1: Write failing Domain model tests**

Add to `tests/core/test_models.py`, after the existing imports:

```python
from src.core.models.domain import Domain
```

Add after the last existing test class:

```python
class TestDomainModel:
    def test_create_domain_with_defaults(self):
        d = Domain(name="example.com")
        assert d.name == "example.com"
        assert d.min_interval == 1.0
        assert d.max_concurrency == 2
        assert d.current_interval == 1.0
        assert d.last_request_at is None

    def test_create_domain_custom(self):
        d = Domain(name="slow.gov", min_interval=5.0, max_concurrency=1, current_interval=10.0)
        assert d.min_interval == 5.0
        assert d.max_concurrency == 1
        assert d.current_interval == 10.0

    def test_current_interval_defaults_to_min_interval(self):
        d = Domain(name="example.com", min_interval=3.0)
        assert d.current_interval == 3.0
```

- [ ] **Step 1.2: Run test to verify it fails**

```
uv run pytest tests/core/test_models.py::TestDomainModel -v
```
Expected: `ImportError` — `domain` not yet defined.

- [ ] **Step 1.3: Create `src/core/models/domain.py`**

```python
"""Domain model — per-domain rate limiter configuration."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_CONCURRENCY = 2


class Domain(Base, TimestampMixin):
    """Per-domain rate limiter configuration and backoff state."""

    __tablename__ = "domains"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    min_interval: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_MIN_INTERVAL, server_default="1.0"
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_CONCURRENCY, server_default="2"
    )
    current_interval: Mapped[float] = mapped_column(
        Float, nullable=False, default=DEFAULT_MIN_INTERVAL, server_default="1.0"
    )
    last_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("min_interval", DEFAULT_MIN_INTERVAL)
        kwargs.setdefault("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        kwargs.setdefault("current_interval", kwargs.get("min_interval", DEFAULT_MIN_INTERVAL))
        super().__init__(**kwargs)
```

- [ ] **Step 1.4: Export Domain from `src/core/models/__init__.py`**

Replace the entire file with:

```python
"""SQLAlchemy models."""

from src.core.models.audit_log import AuditLog
from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.core.models.change import Change
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.notification_config import NotificationConfig
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watch import ContentType, Watch

__all__ = [
    "AuditLog",
    "Base",
    "Change",
    "ContentType",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MIN_INTERVAL",
    "Domain",
    "NotificationConfig",
    "PostAction",
    "ProfileType",
    "Snapshot",
    "SnapshotChunk",
    "TemporalProfile",
    "TimestampMixin",
    "ULIDType",
    "Watch",
    "generate_ulid",
]
```

- [ ] **Step 1.5: Run Domain model tests**

```
uv run pytest tests/core/test_models.py::TestDomainModel -v
```
Expected: 3 passed.

- [ ] **Step 1.6: Write failing Watch column tests**

Add to `TestWatchModel` in `tests/core/test_models.py`:

```python
    def test_watch_effective_fields_default_none(self):
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        assert watch.effective_url is None
        assert watch.effective_domain is None
```

- [ ] **Step 1.7: Run test to verify it fails**

```
uv run pytest "tests/core/test_models.py::TestWatchModel::test_watch_effective_fields_default_none" -v
```
Expected: `AttributeError` — fields not yet defined.

- [ ] **Step 1.8: Add `effective_url` and `effective_domain` to `src/core/models/watch.py`**

Add after the `last_checked_at` column (inside the `Watch` class body):
```python
    effective_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    effective_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, default=None)
```

`String` is already imported; `Text` is already imported. No new imports needed.

- [ ] **Step 1.9: Run Watch column test**

```
uv run pytest "tests/core/test_models.py::TestWatchModel::test_watch_effective_fields_default_none" -v
```
Expected: 1 passed.

- [ ] **Step 1.10: Generate Alembic migration**

```bash
export $(cat env | xargs)
uv run alembic revision --autogenerate -m "add domains table and watch effective fields"
```

Open the generated file in `alembic/versions/`. Verify `upgrade()` includes:
- `op.create_table("domains", ...)` with all columns
- `op.add_column("watches", sa.Column("effective_url", ...))`
- `op.add_column("watches", sa.Column("effective_domain", ...))`

Verify `downgrade()` includes:
```python
op.drop_column("watches", "effective_domain")
op.drop_column("watches", "effective_url")
op.drop_table("domains")
```

If any columns are missing, add them manually.

- [ ] **Step 1.11: Apply migration**

```bash
export $(cat env | xargs)
uv run alembic upgrade head
```
Expected: No errors.

- [ ] **Step 1.12: Run full test suite**

```
uv run pytest -x -q
```
Expected: All previously passing tests still pass.

- [ ] **Step 1.13: Commit**

```bash
git add src/core/models/domain.py src/core/models/__init__.py src/core/models/watch.py \
    tests/core/test_models.py alembic/versions/
git commit -m "#30 feat: add Domain model and watch effective_url/domain columns"
```

---

## Task 2: DomainRateLimiter enhancements + shared singleton

Adds `get_rate_limiter()` singleton factory, `configure_domain()`, `acquire_for_domain()`, and `report_rate_limited_for_domain()`. Moving the singleton to `rate_limiter.py` ensures `main.py` and `tasks.py` share the same instance.

**Files:**
- Modify: `src/core/rate_limiter.py`
- Modify: `src/workers/tasks.py` (remove local singleton, import from `rate_limiter`)
- Modify: `tests/core/test_rate_limiter.py`

---

- [ ] **Step 2.1: Write failing tests**

Add to `tests/core/test_rate_limiter.py`:

```python
class TestConfigureDomain:
    def test_configure_domain_stores_current_interval_as_effective_rate(self):
        """configure_domain stores current_interval in state.min_interval.

        DomainState only has min_interval — it is the effective rate used during
        acquire. configure_domain loads current_interval here so that backoff
        state persists across restarts. The operator min_interval floor is stored
        in the DB only; in-memory state just tracks the effective rate.
        """
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            min_interval=2.0,
            max_concurrency=1,
            current_interval=5.0,
        )
        state = limiter._domains["example.com"]
        assert state.min_interval == 5.0  # current_interval, not min_interval arg

    def test_configure_domain_sets_concurrency(self):
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            min_interval=1.0,
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
            min_interval=0.1,
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
```

Also add a singleton test at module level (not inside the class):

```python
class TestGetRateLimiter:
    def test_get_rate_limiter_returns_same_instance(self):
        from src.core.rate_limiter import get_rate_limiter, reset_rate_limiter
        reset_rate_limiter()
        a = get_rate_limiter()
        b = get_rate_limiter()
        assert a is b
        reset_rate_limiter()  # clean up
```

- [ ] **Step 2.2: Run tests to verify they fail**

```
uv run pytest tests/core/test_rate_limiter.py::TestConfigureDomain \
    tests/core/test_rate_limiter.py::TestGetRateLimiter -v
```
Expected: `AttributeError` — methods not yet defined.

- [ ] **Step 2.3: Add methods and singleton to `src/core/rate_limiter.py`**

Add the three new methods **inside the `DomainRateLimiter` class body** (before the closing of the class, after the existing `report_rate_limited` method):

```python
    def configure_domain(
        self,
        name: str,
        min_interval: float,
        max_concurrency: int,
        current_interval: float,
    ) -> None:
        """Hydrate in-memory state from a persisted Domain record.

        Loads current_interval as the effective rate (state.min_interval) so
        backoff state survives restarts. The operator-configured min_interval
        floor is stored in the DB; in-memory DomainState only tracks the
        current effective rate.
        """
        self._domains[name] = DomainState(
            semaphore=asyncio.Semaphore(max_concurrency),
            min_interval=current_interval,
        )

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
                if elapsed < state.min_interval:
                    await asyncio.sleep(state.min_interval - elapsed)
                state.last_request_at = time.monotonic()
            yield
        finally:
            state.semaphore.release()

    def report_rate_limited_for_domain(self, domain: str) -> float:
        """Report a 429 for a known domain name; return the new interval.

        Use instead of report_rate_limited(url) when effective_domain is known.
        """
        state = self._domains[domain]
        new_interval = max(state.min_interval * BACKOFF_MULTIPLIER, 2.0)
        state.min_interval = min(new_interval, BACKOFF_MAX_INTERVAL)
        logger.warning(
            "rate limited, increasing interval",
            extra={"domain": domain, "new_interval": state.min_interval},
        )
        return state.min_interval
```

Then add the module-level singleton at the bottom of the file (after the class):

```python
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
```

- [ ] **Step 2.4: Update `src/workers/tasks.py` — use shared singleton**

Replace the existing local `_rate_limiter` / `get_rate_limiter` definitions in `tasks.py` with an import:

Remove these lines from `tasks.py`:
```python
_rate_limiter: DomainRateLimiter | None = None


def get_rate_limiter() -> DomainRateLimiter:
    """Return the shared rate limiter, creating it on first call."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = DomainRateLimiter()
    return _rate_limiter
```

Add to the imports at the top of `tasks.py`:
```python
from src.core.rate_limiter import DomainRateLimiter, get_rate_limiter
```

(The `DomainRateLimiter` import is already there via `from src.core.rate_limiter import DomainRateLimiter` — merge into one line.)

- [ ] **Step 2.5: Run all rate limiter tests**

```
uv run pytest tests/core/test_rate_limiter.py -v
```
Expected: All pass (including existing tests).

- [ ] **Step 2.6: Run full suite**

```
uv run pytest -x -q
```
Expected: All pass.

- [ ] **Step 2.7: Commit**

```bash
git add src/core/rate_limiter.py src/workers/tasks.py tests/core/test_rate_limiter.py
git commit -m "#30 feat: add configure_domain, acquire_for_domain, report_rate_limited_for_domain; move singleton to rate_limiter.py"
```

---

## Task 3: URL probe logic

**Files:**
- Create: `src/core/probe.py`
- Create: `tests/core/test_probe.py`

---

- [ ] **Step 3.1: Write failing tests**

Create `tests/core/test_probe.py`:

```python
"""Unit tests for URL probe logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.probe import ProbeResult, probe_url


class TestProbeResult:
    def test_probe_result_fields(self):
        r = ProbeResult(
            effective_url="https://example.com/page",
            effective_domain="example.com",
            redirect_chain=["https://www.example.com/page", "https://example.com/page"],
            status_code=200,
            content_type="text/html; charset=utf-8",
        )
        assert r.effective_domain == "example.com"
        assert len(r.redirect_chain) == 2


class TestProbeUrl:
    async def test_no_redirect(self):
        mock_response = MagicMock()
        mock_response.url = httpx.URL("https://example.com/page")
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.history = []

        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await probe_url("https://example.com/page")

        assert result.effective_url == "https://example.com/page"
        assert result.effective_domain == "example.com"
        assert result.redirect_chain == ["https://example.com/page"]
        assert result.status_code == 200

    async def test_redirect_followed(self):
        redirect_response = MagicMock()
        redirect_response.url = httpx.URL("https://www.example.com/page")
        redirect_response.status_code = 301

        final_response = MagicMock()
        final_response.url = httpx.URL("https://example.com/page")
        final_response.status_code = 200
        final_response.headers = {"content-type": "text/html"}
        final_response.history = [redirect_response]

        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(return_value=final_response)
            mock_client_cls.return_value = mock_client

            result = await probe_url("https://www.example.com/page")

        assert result.effective_url == "https://example.com/page"
        assert result.effective_domain == "example.com"
        assert result.redirect_chain == [
            "https://www.example.com/page",
            "https://example.com/page",
        ]

    async def test_connection_error_raises(self):
        with patch("src.core.probe.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.head = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.ConnectError):
                await probe_url("https://unreachable.example.com/")
```

- [ ] **Step 3.2: Run tests to verify they fail**

```
uv run pytest tests/core/test_probe.py -v
```
Expected: `ImportError` — `src.core.probe` not yet defined.

- [ ] **Step 3.3: Create `src/core/probe.py`**

```python
"""URL probe — resolve effective URL and domain by following redirects."""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from src.core.logging import get_logger

logger = get_logger(__name__)

PROBE_TIMEOUT = 15.0
PROBE_USER_AGENT = "watcher/0.1.0 (probe)"


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
```

- [ ] **Step 3.4: Run probe tests**

```
uv run pytest tests/core/test_probe.py -v
```
Expected: All pass.

- [ ] **Step 3.5: Commit**

```bash
git add src/core/probe.py tests/core/test_probe.py
git commit -m "#30 feat: add URL probe logic (ProbeResult + probe_url)"
```

---

## Task 4: Domain API — schemas and routes

**Files:**
- Create: `src/api/schemas/domain.py`
- Create: `src/api/routes/domains.py`
- Create: `tests/api/test_domains.py`

---

- [ ] **Step 4.1: Write failing domain API tests**

Create `tests/api/test_domains.py`:

```python
"""Integration tests for domain API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestGetDomains:
    async def test_list_domains_empty(self, client):
        response = await client.get("/api/domains")
        assert response.status_code == 200
        assert response.json() == []

    async def test_get_domain_not_found(self, client):
        response = await client.get("/api/domains/nonexistent.com")
        assert response.status_code == 404


class TestPatchDomain:
    async def test_patch_creates_domain_if_absent(self, client):
        response = await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 3.0, "max_concurrency": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "example.com"
        assert data["min_interval"] == 3.0
        assert data["max_concurrency"] == 1
        assert data["current_interval"] == 3.0  # defaults to min_interval on create

    async def test_patch_updates_existing_domain(self, client):
        await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 2.0, "max_concurrency": 2},
        )
        response = await client.patch(
            "/api/domains/example.com",
            json={"min_interval": 5.0},
        )
        assert response.status_code == 200
        assert response.json()["min_interval"] == 5.0
        assert response.json()["max_concurrency"] == 2  # unchanged

    async def test_patch_with_no_fields_returns_current(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.patch("/api/domains/example.com", json={})
        assert response.status_code == 200
        assert response.json()["min_interval"] == 2.0

    async def test_list_includes_patched_domain(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.get("/api/domains")
        assert response.status_code == 200
        names = [d["name"] for d in response.json()]
        assert "example.com" in names

    async def test_response_includes_id(self, client):
        response = await client.patch("/api/domains/example.com", json={})
        assert "id" in response.json()


class TestGetDomainByName:
    async def test_get_existing_domain(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 2.0})
        response = await client.get("/api/domains/example.com")
        assert response.status_code == 200
        assert response.json()["name"] == "example.com"


class TestDeleteDomain:
    async def test_delete_orphaned_domain_returns_204(self, client):
        await client.patch("/api/domains/orphan.com", json={"min_interval": 1.0})
        response = await client.delete("/api/domains/orphan.com")
        assert response.status_code == 204

    async def test_delete_nonexistent_returns_404(self, client):
        response = await client.delete("/api/domains/nope.com")
        assert response.status_code == 404

    @pytest.mark.xfail(strict=False, reason="requires probe in watch creation (Task 6)")
    async def test_delete_domain_with_watches_returns_409(self, client):
        await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        response = await client.delete("/api/domains/example.com")
        assert response.status_code == 409
        assert "watches" in response.json()["detail"].lower()
```

- [ ] **Step 4.2: Run tests to verify they fail**

```
uv run pytest tests/api/test_domains.py -v
```
Expected: 404 for all endpoints (routes not registered yet).

- [ ] **Step 4.3: Create `src/api/schemas/domain.py`**

```python
"""Pydantic schemas for Domain API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.api.schemas.types import ULIDStr


class DomainPatch(BaseModel):
    """Schema for creating or updating a domain config (upsert via PATCH)."""

    min_interval: float | None = None
    max_concurrency: int | None = None


class DomainResponse(BaseModel):
    """Schema for returning a domain config."""

    model_config = ConfigDict(from_attributes=True)

    id: ULIDStr
    name: str
    min_interval: float
    max_concurrency: int
    current_interval: float
    last_request_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4.4: Create `src/api/routes/domains.py`**

```python
"""Domain rate limiter config API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.schemas.domain import DomainPatch, DomainResponse
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/domains", tags=["domains"])


async def _get_domain_or_404(name: str, session: AsyncSession) -> Domain:
    """Fetch a domain by name, raising 404 if not found."""
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return domain


@router.get("", response_model=list[DomainResponse])
async def list_domains(session: AsyncSession = Depends(get_db_session)):
    """List all domain configs."""
    result = await session.execute(select(Domain).order_by(Domain.name))
    return result.scalars().all()


@router.get("/{name}", response_model=DomainResponse)
async def get_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Get a domain config by hostname."""
    return await _get_domain_or_404(name, session)


@router.patch("/{name}", response_model=DomainResponse)
async def upsert_domain(
    name: str,
    data: DomainPatch,
    session: AsyncSession = Depends(get_db_session),
):
    """Create or update a domain config (upsert by hostname).

    On create: min_interval defaults to 1.0, current_interval defaults to min_interval.
    On update: only provided fields are changed.
    """
    stmt = select(Domain).where(Domain.name == name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()

    updates = data.model_dump(exclude_unset=True)

    if domain is None:
        min_iv = updates.get("min_interval", DEFAULT_MIN_INTERVAL)
        domain = Domain(
            name=name,
            min_interval=min_iv,
            max_concurrency=updates.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
            current_interval=min_iv,
        )
        session.add(domain)
    else:
        if "min_interval" in updates:
            domain.min_interval = updates["min_interval"]
        if "max_concurrency" in updates:
            domain.max_concurrency = updates["max_concurrency"]

    await session.commit()
    await session.refresh(domain)
    return domain


@router.delete("/{name}", status_code=204)
async def delete_domain(name: str, session: AsyncSession = Depends(get_db_session)):
    """Delete a domain config.

    Returns 409 if any watches still reference this domain as their effective_domain.
    """
    domain = await _get_domain_or_404(name, session)

    stmt = select(Watch).where(Watch.effective_domain == name).limit(1)
    result = await session.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watches still reference domain '{name}'",
        )

    await session.delete(domain)
    await session.commit()
```

- [ ] **Step 4.5: Register the domain router in `src/api/main.py`**

Add these lines (router registration; full main.py rewrite is in Task 7):

```python
from src.api.routes.domains import router as domains_router
# ...
app.include_router(domains_router)
```

- [ ] **Step 4.6: Run domain tests**

```
uv run pytest tests/api/test_domains.py -v
```
Expected: All pass except the xfail (shown as `xfail`, not `FAILED`).

- [ ] **Step 4.7: Run full suite**

```
uv run pytest -x -q
```
Expected: No regressions.

- [ ] **Step 4.8: Commit**

```bash
git add src/api/schemas/domain.py src/api/routes/domains.py \
    src/api/main.py tests/api/test_domains.py
git commit -m "#30 feat: add domain CRUD API (GET/PATCH/DELETE /api/domains)"
```

---

## Task 5: Probe endpoint + dependency injection

**Files:**
- Create: `src/api/routes/probe.py`
- Modify: `src/api/dependencies.py`
- Create: `tests/api/test_probe.py`
- Modify: `tests/conftest.py`

---

- [ ] **Step 5.1: Write failing probe endpoint tests**

Create `tests/api/test_probe.py`:

```python
"""Integration tests for POST /api/probe."""

import pytest

pytestmark = pytest.mark.integration


class TestProbeEndpoint:
    async def test_probe_returns_effective_url(self, client):
        # conftest mock probe returns URL as-is (no redirect)
        response = await client.post(
            "/api/probe", json={"url": "https://example.com/page"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"
        assert isinstance(data["redirect_chain"], list)
        assert data["status_code"] == 200

    async def test_probe_missing_url_returns_422(self, client):
        response = await client.post("/api/probe", json={})
        assert response.status_code == 422
```

- [ ] **Step 5.2: Run tests to verify they fail**

```
uv run pytest tests/api/test_probe.py -v
```
Expected: 404 (route not registered yet).

- [ ] **Step 5.3: Add `get_probe_fn` to `src/api/dependencies.py`**

Add the following to the existing file (do not remove `get_db_session`):

```python
from collections.abc import Awaitable, Callable
from src.core.probe import ProbeResult, probe_url

async def get_probe_fn() -> Callable[[str], Awaitable[ProbeResult]]:
    """Return the URL probe function. Override in tests to avoid real HTTP calls."""
    return probe_url
```

The full file after the addition:

```python
"""FastAPI dependencies — database session injection."""

from collections.abc import AsyncGenerator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session_factory
from src.core.probe import ProbeResult, probe_url


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with get_session_factory()() as session:
        yield session


async def get_probe_fn() -> Callable[[str], Awaitable[ProbeResult]]:
    """Return the URL probe function. Override in tests to avoid real HTTP calls."""
    return probe_url
```

- [ ] **Step 5.4: Create `src/api/routes/probe.py`**

```python
"""URL probe endpoint — resolve effective URL and domain."""

from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.dependencies import get_probe_fn
from src.core.probe import ProbeResult

router = APIRouter(prefix="/api/probe", tags=["probe"])


class ProbeRequest(BaseModel):
    """Input schema for the probe endpoint."""

    url: str


class ProbeResponse(BaseModel):
    """Output schema for the probe endpoint."""

    effective_url: str
    effective_domain: str
    redirect_chain: list[str]
    status_code: int
    content_type: str | None


@router.post("", response_model=ProbeResponse)
async def probe_endpoint(
    data: ProbeRequest,
    probe_fn: Annotated[Callable[[str], Awaitable[ProbeResult]], Depends(get_probe_fn)],
) -> ProbeResponse:
    """Probe a URL: follow redirects, return effective URL and domain."""
    try:
        result: ProbeResult = await probe_fn(data.url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

    return ProbeResponse(
        effective_url=result.effective_url,
        effective_domain=result.effective_domain,
        redirect_chain=result.redirect_chain,
        status_code=result.status_code,
        content_type=result.content_type,
    )
```

- [ ] **Step 5.5: Update `tests/conftest.py` — add mock probe override**

Add after the existing imports at the top of the file:

```python
from src.api.dependencies import get_probe_fn
from src.core.probe import ProbeResult
```

Add this helper function before the fixtures:

```python
def _make_mock_probe():
    """Return a mock probe that resolves URLs without real HTTP calls."""
    from urllib.parse import urlparse

    async def mock_probe(url: str) -> ProbeResult:
        hostname = urlparse(url).hostname or ""
        return ProbeResult(
            effective_url=url,
            effective_domain=hostname,
            redirect_chain=[url],
            status_code=200,
            content_type="text/html",
        )

    return mock_probe
```

Replace the `client` fixture with:

```python
@pytest.fixture
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient]:
    from src.api.dependencies import get_db_session, get_probe_fn
    from src.api.main import app

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    async def override_probe_fn():
        return _make_mock_probe()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_probe_fn] = override_probe_fn
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

- [ ] **Step 5.6: Register probe router in `src/api/main.py`**

Add:
```python
from src.api.routes.probe import router as probe_router
# ...
app.include_router(probe_router)
```

- [ ] **Step 5.7: Run probe and full suite**

```
uv run pytest tests/api/test_probe.py tests/core/test_probe.py -v
uv run pytest -x -q
```
Expected: All pass; no regressions.

- [ ] **Step 5.8: Commit**

```bash
git add src/api/dependencies.py src/api/routes/probe.py \
    src/api/main.py tests/api/test_probe.py tests/conftest.py
git commit -m "#30 feat: add POST /api/probe endpoint and probe dependency injection"
```

---

## Task 6: Watch creation integration

Wires probe into `POST /api/watches`, upserts domain, stores `effective_url`/`effective_domain`. Updates schemas.

**Files:**
- Modify: `src/api/routes/watches.py`
- Modify: `src/api/schemas/watch.py`
- Modify: `tests/api/test_watches.py`
- Modify: `tests/api/test_domains.py` (remove xfail)

---

- [ ] **Step 6.1: Write failing watch integration tests**

Add to `tests/api/test_watches.py`:

```python
class TestCreateWatchProbe:
    async def test_create_watch_populates_effective_fields(self, client):
        response = await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/page", "content_type": "html"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"

    async def test_create_watch_upserts_domain(self, client):
        await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        domains = (await client.get("/api/domains")).json()
        assert any(d["name"] == "example.com" for d in domains)

    async def test_create_watch_does_not_overwrite_existing_domain_config(self, client):
        await client.patch("/api/domains/example.com", json={"min_interval": 10.0})
        await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        domain = (await client.get("/api/domains/example.com")).json()
        assert domain["min_interval"] == 10.0  # operator config preserved

    async def test_patch_watch_url_is_unchanged(self, client):
        resp = await client.post(
            "/api/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}", json={"name": "Updated"}
        )
        assert response.status_code == 200
        assert response.json()["url"] == "https://example.com/p"  # unchanged
```

- [ ] **Step 6.2: Run tests to verify they fail**

```
uv run pytest tests/api/test_watches.py::TestCreateWatchProbe -v
```
Expected: Fail — `effective_url`/`effective_domain` not in response yet.

- [ ] **Step 6.3: Update `src/api/schemas/watch.py`**

Add to `WatchResponse`:
```python
    effective_url: str | None = None
    effective_domain: str | None = None
```

Replace `WatchUpdate` entirely:
```python
class WatchUpdate(BaseModel):
    """Schema for updating a watch. All fields optional. URL is immutable after creation."""

    name: str | None = None
    # url intentionally omitted — URL cannot change; delete and recreate to change
    content_type: ContentType | None = None
    fetch_config: dict | None = None
    schedule_config: dict | None = None
    is_active: bool | None = None
```

- [ ] **Step 6.4: Update `src/api/routes/watches.py` — integrate probe and domain upsert**

Add to the imports at the top (with existing imports):
```python
from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx

from src.api.dependencies import get_db_session, get_probe_fn
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
```

Replace `create_watch`:

```python
@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    session: AsyncSession = Depends(get_db_session),
    probe_fn: Annotated[Callable[[str], Awaitable], Depends(get_probe_fn)],
):
    """Create a new watch. Probes the URL to resolve effective domain."""
    try:
        probe_result = await probe_fn(data.url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

    # Upsert domain — insert with defaults if new, leave config intact if exists
    domain_stmt = select(Domain).where(Domain.name == probe_result.effective_domain)
    domain_result = await session.execute(domain_stmt)
    if not domain_result.scalar_one_or_none():
        session.add(Domain(
            name=probe_result.effective_domain,
            min_interval=DEFAULT_MIN_INTERVAL,
            max_concurrency=DEFAULT_MAX_CONCURRENCY,
            current_interval=DEFAULT_MIN_INTERVAL,
        ))

    watch = Watch(
        name=data.name,
        url=data.url,
        content_type=data.content_type,
        fetch_config=data.fetch_config,
        schedule_config=data.schedule_config,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    session.add(watch)
    await session.flush()
    audit = AuditLog(
        event_type="watch.created",
        watch_id=watch.id,
        payload={
            "name": data.name,
            "url": data.url,
            "content_type": data.content_type.value,
            "effective_url": probe_result.effective_url,
            "effective_domain": probe_result.effective_domain,
        },
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch
```

- [ ] **Step 6.5: Remove xfail from domain test**

In `tests/api/test_domains.py`, remove `@pytest.mark.xfail(...)` from `test_delete_domain_with_watches_returns_409`.

- [ ] **Step 6.6: Run watch and domain tests**

```
uv run pytest tests/api/test_watches.py tests/api/test_domains.py -v
```
Expected: All pass.

- [ ] **Step 6.7: Run full suite**

```
uv run pytest -x -q
```
Expected: All pass.

- [ ] **Step 6.8: Commit**

```bash
git add src/api/schemas/watch.py src/api/routes/watches.py \
    tests/api/test_watches.py tests/api/test_domains.py
git commit -m "#30 feat: integrate probe into watch creation; upsert domain on watch create"
```

---

## Task 7: App wiring — startup hydration + worker backoff persistence

**Files:**
- Modify: `src/api/main.py`
- Modify: `src/workers/tasks.py`
- Create: `tests/core/test_hydrate.py`
- Create: `tests/workers/__init__.py`
- Create: `tests/workers/test_tasks.py`

---

- [ ] **Step 7.1: Write failing test for `hydrate_rate_limiter`**

Create `tests/core/test_hydrate.py`:

```python
"""Unit tests for rate limiter startup hydration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.rate_limiter import DomainRateLimiter, reset_rate_limiter


async def test_hydrate_rate_limiter_loads_domains():
    from src.api.main import hydrate_rate_limiter
    from src.core.models.domain import Domain

    limiter = DomainRateLimiter()

    d1 = Domain(name="example.com", min_interval=2.0, max_concurrency=1, current_interval=4.0)
    d2 = Domain(name="other.gov", min_interval=5.0, max_concurrency=2, current_interval=5.0)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [d1, d2]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.api.main.get_session_factory", return_value=MagicMock(return_value=mock_session)):
        await hydrate_rate_limiter(limiter)

    assert limiter._domains["example.com"].min_interval == 4.0  # current_interval loaded
    assert limiter._domains["example.com"].semaphore._value == 1
    assert limiter._domains["other.gov"].min_interval == 5.0


async def test_hydrate_rate_limiter_empty_db():
    from src.api.main import hydrate_rate_limiter

    limiter = DomainRateLimiter()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.api.main.get_session_factory", return_value=MagicMock(return_value=mock_session)):
        await hydrate_rate_limiter(limiter)

    assert len(limiter._domains) == 0
```

- [ ] **Step 7.2: Run tests to verify they fail**

```
uv run pytest tests/core/test_hydrate.py -v
```
Expected: `ImportError` — `hydrate_rate_limiter` not yet defined.

- [ ] **Step 7.3: Write failing test for backoff persistence helper**

Create `tests/workers/__init__.py` (empty file), then create `tests/workers/test_tasks.py`:

```python
"""Unit tests for worker task helpers."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workers.tasks import _persist_backoff


class TestPersistBackoff:
    async def test_persist_backoff_updates_domain(self):
        domain = MagicMock()
        domain.current_interval = 1.0
        domain.last_request_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        before = datetime.now(UTC)
        await _persist_backoff("example.com", 4.0, mock_session)

        assert domain.current_interval == 4.0
        assert domain.last_request_at >= before

    async def test_persist_backoff_noop_if_domain_missing(self):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Should not raise
        await _persist_backoff("unknown.com", 4.0, mock_session)
```

- [ ] **Step 7.4: Run test to verify it fails**

```
uv run pytest tests/workers/test_tasks.py -v
```
Expected: `ImportError` — `_persist_backoff` not yet defined.

- [ ] **Step 7.5: Rewrite `src/api/main.py`**

Note: `hydrate_rate_limiter` must call `get_rate_limiter()` from `src.core.rate_limiter` — the same singleton used by `tasks.py`. Do NOT create a separate `DomainRateLimiter` instance in main.py.

```python
"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from src.api.routes.audit_log import router as audit_router
from src.api.routes.changes import router as changes_router
from src.api.routes.domains import router as domains_router
from src.api.routes.notification_configs import router as notification_configs_router
from src.api.routes.probe import router as probe_router
from src.api.routes.temporal_profiles import router as profiles_router
from src.api.routes.watches import router as watches_router
from src.core.database import get_session_factory
from src.core.logging import configure_logging, get_logger
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter, get_rate_limiter
from src.dashboard import register_dashboard

configure_logging()
logger = get_logger(__name__)


async def hydrate_rate_limiter(limiter: DomainRateLimiter) -> None:
    """Load persisted domain configs into the rate limiter at startup."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Domain))
        domains = result.scalars().all()
    for d in domains:
        limiter.configure_domain(
            name=d.name,
            min_interval=d.min_interval,
            max_concurrency=d.max_concurrency,
            current_interval=d.current_interval,
        )
    logger.info("rate limiter hydrated", extra={"domain_count": len(domains)})


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Hydrate rate limiter and start procrastinate worker at startup."""
    from src.workers import get_app

    await hydrate_rate_limiter(get_rate_limiter())

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(proc_app.run_worker_async(install_signal_handlers=False))
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await proc_app.close_async()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)
app.include_router(watches_router)
app.include_router(changes_router)
app.include_router(profiles_router)
app.include_router(notification_configs_router)
app.include_router(audit_router)
app.include_router(domains_router)
app.include_router(probe_router)
register_dashboard(app)
```

- [ ] **Step 7.6: Add `_persist_backoff` helper and update `check_watch` in `src/workers/tasks.py`**

Add to the imports at the top of `tasks.py` (with existing imports):
```python
from datetime import UTC, datetime
from sqlalchemy import select
from src.core.models.domain import Domain
```

(`from datetime import UTC, datetime` may already be partially imported — merge with any existing datetime imports into one line.)

Add after the `_EXTRACTOR_MAP` block:

```python
async def _persist_backoff(domain_name: str, new_interval: float, session: AsyncSession) -> None:
    """Persist backoff state to the Domain table after a 429 response.

    Caller is responsible for committing the session after this call.
    """
    stmt = select(Domain).where(Domain.name == domain_name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if domain:
        domain.current_interval = new_interval
        domain.last_request_at = datetime.now(UTC)
```

In `check_watch`, replace the rate limiting + 429 block:

```python
        # Use effective_domain if resolved; fall back to URL parsing for old watches
        rate_limit_domain = watch.effective_domain or get_rate_limiter().extract_domain(watch.url)

        async with get_rate_limiter().acquire_for_domain(rate_limit_domain):
            fetch_result = await get_fetcher().fetch(watch.url, config=fetch_config)

        if fetch_result.status_code == 429:
            new_interval = get_rate_limiter().report_rate_limited_for_domain(rate_limit_domain)
            await _persist_backoff(rate_limit_domain, new_interval, session)
            await session.commit()
            raise ConnectionError(f"Rate limited by {rate_limit_domain}")
```

- [ ] **Step 7.7: Run all new tests**

```
uv run pytest tests/core/test_hydrate.py tests/workers/test_tasks.py -v
```
Expected: All pass.

- [ ] **Step 7.8: Run full suite**

```
uv run pytest -x -q
```
Expected: All tests pass.

- [ ] **Step 7.9: Run linter**

```
uv run ruff check .
```
Fix any issues.

- [ ] **Step 7.10: Commit**

```bash
git add src/api/main.py src/workers/tasks.py \
    tests/core/test_hydrate.py tests/workers/__init__.py tests/workers/test_tasks.py
git commit -m "#30 feat: hydrate rate limiter from DB at startup; persist backoff on 429"
```

---

## Completion Checklist

- [ ] All tests pass: `uv run pytest -q`
- [ ] No lint errors: `uv run ruff check .`
- [ ] Migration applied: `uv run alembic upgrade head`
- [ ] `GET /api/domains` lists domain configs
- [ ] `PATCH /api/domains/{name}` upserts (creates or updates)
- [ ] `DELETE /api/domains/{name}` 409s when watches reference the domain
- [ ] `POST /api/probe` returns effective URL and domain
- [ ] Watch creation probes URL, stores effective fields, upserts domain
- [ ] App startup hydrates the shared rate limiter singleton from DB
- [ ] Worker uses `effective_domain` and `acquire_for_domain`; persists backoff on 429
