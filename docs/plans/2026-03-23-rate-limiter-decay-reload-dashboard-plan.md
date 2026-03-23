# Rate Limiter Decay, Reload, Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add time-based backoff decay, poll-based config hot-reload, and a dashboard domains view to the rate limiter.

**Architecture:** Split `DomainState.min_interval` into `min_interval` (operator floor) + `current_interval` (effective, backoff-adjusted rate). Add `decay_window` column to `domains`. Background poller syncs DB config into memory every 60s. New dashboard page shows domain state with watch counts and backoff indicators.

**Tech Stack:** Python 3.12, SQLAlchemy (async), Alembic, FastAPI, Jinja2 + HTMX + Tailwind, pytest

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `src/core/rate_limiter.py` | Split DomainState, add decay methods, update acquire/backoff to use `current_interval` |
| Modify | `src/core/models/domain.py` | Add `decay_window` column |
| Modify | `src/api/schemas/domain.py` | Add `decay_window` to DomainPatch and DomainResponse |
| Modify | `src/api/routes/domains.py` | Handle `decay_window` in upsert |
| Create | `src/core/config_poller.py` | Background polling task |
| Modify | `src/api/main.py` | Start/stop poller in lifespan, update hydrate to pass min_interval |
| Modify | `src/workers/pipeline.py` | Add `_maybe_decay_backoff()` |
| Modify | `src/workers/tasks.py` | Call `_maybe_decay_backoff()` after successful fetch |
| Modify | `src/dashboard/context.py` | Add `get_domains_with_watch_counts()` |
| Modify | `src/dashboard/routes.py` | Add domains page + partial routes |
| Create | `src/dashboard/templates/pages/domains.html` | Full page |
| Create | `src/dashboard/templates/partials/domains_table.html` | HTMX partial |
| Modify | `src/dashboard/templates/base.html` | Add "Domains" nav link |
| Create | alembic migration | Add `decay_window` column |
| Modify | `tests/core/test_rate_limiter.py` | Tests for split state, decay, get_domain_states |
| Modify | `tests/core/test_hydrate.py` | Update hydration tests for new signature |
| Create | `tests/core/test_config_poller.py` | Poller unit tests |
| Modify | `tests/workers/test_tasks.py` | Decay integration test |
| Modify | `tests/api/test_domains.py` | decay_window in API tests |
| Create | `tests/dashboard/test_context.py` | Dashboard context query tests |

---

### Task 1: Split DomainState into min_interval + current_interval

**Files:**
- Modify: `src/core/rate_limiter.py`
- Test: `tests/core/test_rate_limiter.py`

- [ ] **Step 1: Write failing tests for the split state**

Add a new test class to `tests/core/test_rate_limiter.py`:

```python
class TestDomainStateSplit:
    def test_configure_domain_stores_both_intervals(self):
        """configure_domain should store min_interval and current_interval separately."""
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=2,
            min_interval=1.0,
            current_interval=5.0,
        )
        state = limiter._domains["example.com"]
        assert state.min_interval == 1.0
        assert state.current_interval == 5.0

    def test_acquire_uses_current_interval_not_min(self):
        """Throttling should use current_interval, not min_interval."""
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=2,
            min_interval=0.0,
            current_interval=0.1,
        )
        state = limiter._domains["example.com"]
        assert state.current_interval == 0.1

    def test_backoff_increases_current_interval_not_min(self):
        """report_rate_limited_for_domain should increase current_interval, leaving min_interval unchanged."""
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=2,
            min_interval=1.0,
            current_interval=1.0,
        )
        limiter.report_rate_limited_for_domain("example.com")
        state = limiter._domains["example.com"]
        assert state.min_interval == 1.0  # unchanged
        assert state.current_interval > 1.0  # increased

    def test_get_domain_states_includes_both_intervals(self):
        """get_domain_states should report min_interval and current_interval."""
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=2,
            min_interval=1.0,
            current_interval=4.0,
        )
        states = limiter.get_domain_states()
        assert len(states) == 1
        assert states[0]["min_interval"] == 1.0
        assert states[0]["current_interval"] == 4.0
        assert states[0]["in_backoff"] is True

    def test_get_domain_states_not_in_backoff_when_equal(self):
        """in_backoff should be False when current_interval == min_interval."""
        limiter = DomainRateLimiter()
        limiter.configure_domain(
            name="example.com",
            max_concurrency=2,
            min_interval=1.0,
            current_interval=1.0,
        )
        states = limiter.get_domain_states()
        assert states[0]["in_backoff"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_rate_limiter.py::TestDomainStateSplit -v`
Expected: FAIL — `configure_domain` doesn't accept `min_interval` parameter

- [ ] **Step 3: Implement the DomainState split**

In `src/core/rate_limiter.py`, update `DomainState`:

```python
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
```

Update `DomainRateLimiter.__init__` defaultdict factory to set both:

```python
self._domains: dict[str, DomainState] = defaultdict(
    lambda: DomainState(
        semaphore=asyncio.Semaphore(self._max_concurrent),
        min_interval=self._default_min_interval,
        current_interval=self._default_min_interval,
    )
)
```

Update `acquire` and `acquire_for_domain` to use `state.current_interval` instead of `state.min_interval`:

```python
# In both acquire methods, change:
#   if elapsed < state.min_interval:
#       await asyncio.sleep(state.min_interval - elapsed)
# To:
if elapsed < state.current_interval:
    await asyncio.sleep(state.current_interval - elapsed)
```

Update `report_rate_limited` and `report_rate_limited_for_domain` to modify `current_interval`:

```python
# Change state.min_interval references to state.current_interval:
new_interval = max(state.current_interval * BACKOFF_MULTIPLIER, 2.0)
state.current_interval = min(new_interval, BACKOFF_MAX_INTERVAL)
```

Update `configure_domain` to accept and store both:

```python
def configure_domain(
    self,
    name: str,
    max_concurrency: int,
    min_interval: float,
    current_interval: float,
) -> None:
    """Hydrate in-memory state from a persisted Domain record."""
    self._domains[name] = DomainState(
        semaphore=asyncio.Semaphore(max_concurrency),
        min_interval=min_interval,
        current_interval=current_interval,
    )
```

Update `get_domain_states` to report both intervals:

```python
def get_domain_states(self) -> list[dict]:
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
```

Add `reset_domain_interval` method:

```python
def reset_domain_interval(self, domain: str, min_interval: float) -> None:
    """Reset a domain's current_interval back to min_interval (decay)."""
    if domain in self._domains:
        self._domains[domain].current_interval = min_interval
```

- [ ] **Step 4: Fix existing tests that rely on old DomainState shape**

Update `tests/core/test_rate_limiter.py`:

In `TestConfigureDomain.test_configure_domain_stores_current_interval_as_effective_rate`, update the call:

```python
limiter.configure_domain(
    name="example.com",
    max_concurrency=1,
    min_interval=1.0,
    current_interval=5.0,
)
state = limiter._domains["example.com"]
assert state.current_interval == 5.0
assert state.min_interval == 1.0
```

In `test_configure_domain_sets_concurrency`:

```python
limiter.configure_domain(
    name="example.com",
    max_concurrency=3,
    min_interval=1.0,
    current_interval=1.0,
)
```

In `test_acquire_for_domain_uses_domain_config`:

```python
limiter.configure_domain(
    name="example.com",
    max_concurrency=1,
    min_interval=0.1,
    current_interval=0.1,
)
```

In `test_get_domain_states_reports_backoff`, update assertions:

```python
assert states[0]["current_interval"] > 1.0
# Remove: assert states[0]["interval"] > 1.0
```

In `test_backoff_on_429` (line 65-70), change assertion from `state.min_interval` to `state.current_interval`:

```python
assert state.current_interval > 0.0
```

In `test_report_rate_limited_for_domain_increases_interval` (line 138-142), change assertion:

```python
assert state.current_interval > 1.0  # was: state.min_interval > 1.0
```

In `test_report_rate_limited_for_domain_returns_new_interval` (line 144-147), change assertion:

```python
assert new_interval == limiter._domains["example.com"].current_interval  # was: .min_interval
```

**Update dashboard templates immediately** to avoid broken window (templates reference `domain.interval` which no longer exists after `get_domain_states` changes):

In `src/dashboard/templates/partials/system_health.html`, change `domain.interval` to `domain.current_interval`:

```html
{{ "%.1f"|format(domain.current_interval) }}s{% if domain.in_backoff %} ⚠{% endif %}
```

In `src/dashboard/templates/pages/system.html`, change `domain.interval` to `domain.current_interval`:

```html
<td>{{ "%.1f" | format(domain.current_interval) }}s</td>
```

- [ ] **Step 5: Run all rate limiter tests**

Run: `uv run pytest tests/core/test_rate_limiter.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/core/rate_limiter.py tests/core/test_rate_limiter.py src/dashboard/templates/partials/system_health.html src/dashboard/templates/pages/system.html
git commit -m "#32 refactor: split DomainState into min_interval + current_interval"
```

---

### Task 2: Add decay_window column and migration

**Files:**
- Modify: `src/core/models/domain.py`
- Modify: `src/api/schemas/domain.py`
- Modify: `src/api/routes/domains.py`
- Create: Alembic migration
- Test: `tests/api/test_domains.py`

- [ ] **Step 1: Write failing test for decay_window in API**

Add to `tests/api/test_domains.py`:

```python
class TestDecayWindow:
    async def test_patch_creates_domain_with_default_decay_window(self, client):
        response = await client.patch(
            "/api/v1/domains/decay-test.com",
            json={"min_interval": 2.0},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["decay_window"] == 1800.0

    async def test_patch_sets_custom_decay_window(self, client):
        response = await client.patch(
            "/api/v1/domains/custom-decay.com",
            json={"min_interval": 2.0, "decay_window": 900.0},
        )
        assert response.status_code == 200
        assert response.json()["decay_window"] == 900.0

    async def test_patch_updates_decay_window(self, client):
        await client.patch(
            "/api/v1/domains/update-decay.com",
            json={"min_interval": 1.0},
        )
        response = await client.patch(
            "/api/v1/domains/update-decay.com",
            json={"decay_window": 600.0},
        )
        assert response.status_code == 200
        assert response.json()["decay_window"] == 600.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_domains.py::TestDecayWindow -v`
Expected: FAIL — `decay_window` not in response schema

- [ ] **Step 3: Add decay_window to Domain model**

In `src/core/models/domain.py`, add the constant and column:

```python
DEFAULT_DECAY_WINDOW = 1800.0  # 30 minutes
```

Add to the `Domain` class after `last_request_at`:

```python
decay_window: Mapped[float] = mapped_column(
    Float, nullable=False, default=DEFAULT_DECAY_WINDOW, server_default="1800.0"
)
```

Update `__init__` to set the default:

```python
kwargs.setdefault("decay_window", DEFAULT_DECAY_WINDOW)
```

- [ ] **Step 4: Add decay_window to API schema**

In `src/api/schemas/domain.py`, add to `DomainPatch`:

```python
decay_window: float | None = Field(None, ge=0)
```

Add to `DomainResponse`:

```python
decay_window: float
```

- [ ] **Step 5: Handle decay_window in upsert endpoint**

In `src/api/routes/domains.py`, update the import to include `DEFAULT_DECAY_WINDOW`:

```python
from src.core.models.domain import DEFAULT_DECAY_WINDOW, DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
```

In `upsert_domain`, update the create branch to include `decay_window`:

```python
domain = Domain(
    name=name,
    min_interval=min_iv,
    max_concurrency=updates.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
    current_interval=min_iv,
    decay_window=updates.get("decay_window", DEFAULT_DECAY_WINDOW),
)
```

In the update branch, add:

```python
if "decay_window" in updates:
    domain.decay_window = updates["decay_window"]
```

- [ ] **Step 6: Generate Alembic migration**

Run: `export $(cat env | xargs) && uv run alembic revision --autogenerate -m "add decay_window to domains"`

Review the generated migration — it should add a single column `decay_window` (Float, NOT NULL, server_default='1800.0') to the `domains` table.

- [ ] **Step 7: Apply migration**

Run: `export $(cat env | xargs) && uv run alembic upgrade head`

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/api/test_domains.py -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/core/models/domain.py src/api/schemas/domain.py src/api/routes/domains.py alembic/versions/*decay_window* tests/api/test_domains.py
git commit -m "#32 feat: add decay_window column to domains table"
```

---

### Task 3: Update hydration to pass min_interval

**Files:**
- Modify: `src/api/main.py`
- Test: `tests/core/test_hydrate.py`

- [ ] **Step 1: Update hydration test**

In `tests/core/test_hydrate.py`, update `test_hydrate_rate_limiter_loads_domains`:

```python
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

    mock_factory = MagicMock(return_value=mock_session)
    with patch("src.api.main.get_session_factory", return_value=mock_factory):
        await hydrate_rate_limiter(limiter)

    assert limiter._domains["example.com"].min_interval == 2.0
    assert limiter._domains["example.com"].current_interval == 4.0
    assert limiter._domains["example.com"].semaphore._value == 1
    assert limiter._domains["other.gov"].min_interval == 5.0
    assert limiter._domains["other.gov"].current_interval == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_hydrate.py -v`
Expected: FAIL — `configure_domain` call in `hydrate_rate_limiter` doesn't pass `min_interval`

- [ ] **Step 3: Update hydrate_rate_limiter in main.py**

```python
async def hydrate_rate_limiter(limiter: DomainRateLimiter) -> None:
    """Load persisted domain configs into the rate limiter at startup."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Domain))
        domains = result.scalars().all()
    for d in domains:
        limiter.configure_domain(
            name=d.name,
            max_concurrency=d.max_concurrency,
            min_interval=d.min_interval,
            current_interval=d.current_interval,
        )
    logger.info("rate limiter hydrated", extra={"domain_count": len(domains)})
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_hydrate.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/main.py tests/core/test_hydrate.py
git commit -m "#32 fix: pass min_interval to configure_domain during hydration"
```

---

### Task 4: Implement backoff decay in worker pipeline

**Files:**
- Modify: `src/workers/pipeline.py`
- Modify: `src/workers/tasks.py`
- Modify: `src/core/rate_limiter.py` (add `reset_domain_interval` if not done in Task 1)
- Test: `tests/workers/test_tasks.py`

- [ ] **Step 1: Write failing test for decay helper**

Add to `tests/workers/test_tasks.py`:

```python
from src.workers.pipeline import _maybe_decay_backoff


class TestMaybeDecayBackoff:
    async def test_resets_when_decay_window_exceeded(self):
        """Should reset current_interval to min_interval when last_request_at is old enough."""
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 8.0
        domain.decay_window = 1800.0
        domain.last_request_at = datetime.now(UTC) - timedelta(seconds=1801)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        limiter.configure_domain("example.com", max_concurrency=2, min_interval=1.0, current_interval=8.0)

        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is True
        assert domain.current_interval == 1.0
        assert limiter._domains["example.com"].current_interval == 1.0

    async def test_no_reset_when_within_decay_window(self):
        """Should not reset when last_request_at is recent."""
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 8.0
        domain.decay_window = 1800.0
        domain.last_request_at = datetime.now(UTC) - timedelta(seconds=600)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()
        limiter.configure_domain("example.com", max_concurrency=2, min_interval=1.0, current_interval=8.0)

        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is False
        assert limiter._domains["example.com"].current_interval == 8.0

    async def test_noop_when_not_in_backoff(self):
        """Should do nothing when current_interval == min_interval."""
        domain = MagicMock()
        domain.name = "example.com"
        domain.min_interval = 1.0
        domain.current_interval = 1.0
        domain.decay_window = 1800.0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = domain
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()

        decayed = await _maybe_decay_backoff("example.com", limiter, mock_session)
        assert decayed is False

    async def test_noop_when_domain_not_found(self):
        """Should gracefully handle missing domain row."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        limiter = DomainRateLimiter()

        decayed = await _maybe_decay_backoff("unknown.com", limiter, mock_session)
        assert decayed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/workers/test_tasks.py::TestMaybeDecayBackoff -v`
Expected: FAIL — `_maybe_decay_backoff` does not exist

- [ ] **Step 3: Implement _maybe_decay_backoff in pipeline.py**

Add to `src/workers/pipeline.py`:

```python
from src.core.rate_limiter import DomainRateLimiter


async def _maybe_decay_backoff(
    domain_name: str,
    limiter: DomainRateLimiter,
    session: AsyncSession,
) -> bool:
    """Check if a domain's backoff should decay and reset if so.

    Returns True if decay was applied, False otherwise.
    Caller is responsible for committing the session.
    """
    stmt = select(Domain).where(Domain.name == domain_name)
    result = await session.execute(stmt)
    domain = result.scalar_one_or_none()
    if domain is None:
        return False
    if domain.current_interval <= domain.min_interval:
        return False
    if domain.last_request_at is None:
        return False

    elapsed = (datetime.now(UTC) - domain.last_request_at).total_seconds()
    if elapsed < domain.decay_window:
        return False

    domain.current_interval = domain.min_interval
    limiter.reset_domain_interval(domain_name, domain.min_interval)
    logger.info(
        "backoff decayed",
        extra={"domain": domain_name, "reset_to": domain.min_interval},
    )
    return True
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/workers/test_tasks.py::TestMaybeDecayBackoff -v`
Expected: ALL PASS

- [ ] **Step 5: Wire decay into check_watch**

In `src/workers/tasks.py`, update the import:

```python
from src.workers.pipeline import _maybe_decay_backoff, _persist_backoff, _run_check_pipeline
```

After the successful pipeline run (after `await session.commit()` on line 92), add:

```python
        # Check if domain backoff should decay after successful fetch
        await _maybe_decay_backoff(rate_limit_domain, get_rate_limiter(), session)
        await session.commit()
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/workers/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/workers/pipeline.py src/workers/tasks.py tests/workers/test_tasks.py
git commit -m "#32 feat: add time-based backoff decay after successful fetch"
```

---

### Task 5: Config poller for hot-reload

**Files:**
- Create: `src/core/config_poller.py`
- Modify: `src/api/main.py`
- Create: `tests/core/test_config_poller.py`

- [ ] **Step 1: Write failing tests for config poller**

Create `tests/core/test_config_poller.py`:

```python
"""Tests for config poller — periodic domain config reload."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config_poller import poll_domain_configs
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter


async def test_poll_updates_changed_domains():
    """poll_domain_configs should call configure_domain for rows updated since last_poll."""
    limiter = DomainRateLimiter()

    d1 = Domain(name="changed.com", min_interval=2.0, max_concurrency=3, current_interval=4.0)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [d1]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    assert limiter._domains["changed.com"].min_interval == 2.0
    assert limiter._domains["changed.com"].current_interval == 4.0
    assert limiter._domains["changed.com"].semaphore._value == 3
    assert new_poll > last_poll


async def test_poll_no_changes():
    """poll_domain_configs with no changed rows should not modify limiter."""
    limiter = DomainRateLimiter()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    assert len(limiter._domains) == 0
    assert new_poll > last_poll


async def test_poll_handles_db_error():
    """poll_domain_configs should return last_poll unchanged on DB error."""
    limiter = DomainRateLimiter()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=Exception("DB down"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    # Should return same last_poll so next cycle retries from same point
    assert new_poll == last_poll
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config_poller.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement config_poller.py**

Create `src/core/config_poller.py`:

```python
"""Background config poller — syncs domain configs from DB into rate limiter."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.logging import get_logger
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL = 60  # seconds


async def poll_domain_configs(
    limiter: DomainRateLimiter,
    session_factory,
    last_poll: datetime,
) -> datetime:
    """Poll for domain configs updated since last_poll. Returns new poll timestamp.

    On DB error, logs warning and returns last_poll unchanged (retry next cycle).
    """
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            stmt = select(Domain).where(Domain.updated_at > last_poll)
            result = await session.execute(stmt)
            domains = result.scalars().all()
        for d in domains:
            limiter.configure_domain(
                name=d.name,
                max_concurrency=d.max_concurrency,
                min_interval=d.min_interval,
                current_interval=d.current_interval,
            )
        if domains:
            logger.info("config poller synced domains", extra={"count": len(domains)})
    except Exception:
        logger.warning("config poller DB error, will retry next cycle", exc_info=True)
        return last_poll
    return now


async def start_config_poller(
    limiter: DomainRateLimiter,
    session_factory,
    interval: int = DEFAULT_POLL_INTERVAL,
) -> asyncio.Task:
    """Start background task that polls domain configs every `interval` seconds."""
    async def _poll_loop():
        last_poll = datetime.now(UTC)
        while True:
            await asyncio.sleep(interval)
            last_poll = await poll_domain_configs(limiter, session_factory, last_poll)

    return asyncio.create_task(_poll_loop())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_config_poller.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/config_poller.py tests/core/test_config_poller.py
git commit -m "#32 feat: add config poller for hot-reload of domain configs"
```

---

### Task 6: Integrate poller into app lifespan

**Files:**
- Modify: `src/api/main.py`

- [ ] **Step 1: Update lifespan to start and stop poller**

In `src/api/main.py`, add import:

```python
from src.core.config_poller import start_config_poller
```

Update `lifespan`:

```python
@asynccontextmanager
async def lifespan(application: FastAPI):
    """Hydrate rate limiter, start config poller, and start procrastinate worker."""
    from src.workers import get_app

    limiter = get_rate_limiter()
    await hydrate_rate_limiter(limiter)

    poller_task = await start_config_poller(limiter, get_session_factory())

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(proc_app.run_worker_async(install_signal_handlers=False))
    yield
    poller_task.cancel()
    worker_task.cancel()
    await asyncio.gather(poller_task, worker_task, return_exceptions=True)
    await proc_app.close_async()
```

- [ ] **Step 2: Run full test suite to check no regressions**

Run: `uv run pytest -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "#32 feat: start config poller in app lifespan"
```

---

### Task 7: Dashboard domains page and partial

**Files:**
- Modify: `src/dashboard/context.py`
- Modify: `src/dashboard/routes.py`
- Create: `src/dashboard/templates/pages/domains.html`
- Create: `src/dashboard/templates/partials/domains_table.html`
- Modify: `src/dashboard/templates/base.html`

- [ ] **Step 1: Add get_domains_with_watch_counts to context.py**

Add import at top of `src/dashboard/context.py`:

```python
from src.core.models.domain import Domain
```

Add the query helper:

```python
async def get_domains_with_watch_counts(session: AsyncSession) -> list[dict]:
    """Fetch all domains with watch count per domain."""
    stmt = (
        select(
            Domain,
            func.count(Watch.id).label("watch_count"),
        )
        .outerjoin(Watch, Watch.effective_domain == Domain.name)
        .group_by(Domain.id)
        .order_by(Domain.name)
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "name": domain.name,
            "min_interval": domain.min_interval,
            "current_interval": domain.current_interval,
            "decay_window": domain.decay_window,
            "max_concurrency": domain.max_concurrency,
            "last_request_at": domain.last_request_at,
            "in_backoff": domain.current_interval > domain.min_interval,
            "watch_count": watch_count,
        }
        for domain, watch_count in rows
    ]
```

- [ ] **Step 2: Add dashboard routes**

In `src/dashboard/routes.py`, add import:

```python
from src.dashboard.context import (
    generate_diff,
    get_audit_entries,
    get_change_detail,
    get_dashboard_stats,
    get_domains_with_watch_counts,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_list,
    get_watch_notifications,
    get_watch_profiles,
)
```

Add route handlers before the `partial_stats_cards` handler:

```python
@router.get("/domains")
async def domains_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Domains rate limiter config page."""
    domains = await get_domains_with_watch_counts(session)
    context = {"request": request, "active_page": "domains", "domains": domains}
    return templates.TemplateResponse("pages/domains.html", context)


@router.get("/partials/domains-table")
async def partial_domains_table(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domains table with watch counts and backoff status."""
    domains = await get_domains_with_watch_counts(session)
    return templates.TemplateResponse(
        "partials/domains_table.html", {"request": request, "domains": domains}
    )
```

- [ ] **Step 3: Create domains page template**

Create `src/dashboard/templates/pages/domains.html`:

```html
{% extends "base.html" %}
{% block title %}Domains — watcher{% endblock %}
{% block content %}
<h2 class="text-2xl font-bold text-gray-900 mb-6">Domains</h2>

<div id="domains-table-container"
     hx-get="/partials/domains-table"
     hx-trigger="every 30s"
     hx-swap="innerHTML">
  {% include "partials/domains_table.html" %}
</div>
{% endblock %}
```

- [ ] **Step 4: Create domains table partial**

Create `src/dashboard/templates/partials/domains_table.html`:

```html
{% if domains %}
<div class="overflow-x-auto">
  <table class="data-table">
    <thead>
      <tr>
        <th>Domain</th>
        <th>Min Interval</th>
        <th>Current Interval</th>
        <th>Decay Window</th>
        <th>Concurrency</th>
        <th>Watches</th>
        <th>Last 429</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100">
      {% for domain in domains %}
      <tr class="{% if domain.in_backoff %}bg-red-50{% endif %}">
        <td class="font-medium">{{ domain.name }}</td>
        <td>{{ "%.1f"|format(domain.min_interval) }}s</td>
        <td>{{ "%.1f"|format(domain.current_interval) }}s</td>
        <td>{{ "%.0f"|format(domain.decay_window / 60) }}m</td>
        <td>{{ domain.max_concurrency }}</td>
        <td>{{ domain.watch_count }}</td>
        <td>{{ domain.last_request_at.strftime("%Y-%m-%d %H:%M") if domain.last_request_at else "—" }}</td>
        <td>
          {% if domain.in_backoff %}
          <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">Active</span>
          {% else %}
          <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">Normal</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<p class="text-gray-500 text-sm">No domains configured yet.</p>
{% endif %}
```

- [ ] **Step 5: Add Domains nav link to base.html**

In `src/dashboard/templates/base.html`, add after the Watches link:

```html
<a href="/domains" class="nav-link {% if active_page == 'domains' %}nav-link-active{% endif %}">Domains</a>
```

- [ ] **Step 6: Write test for get_domains_with_watch_counts**

Create `tests/dashboard/test_context.py` (create `tests/dashboard/` directory and `__init__.py` if needed):

```python
"""Tests for dashboard context query helpers."""

import pytest
from sqlalchemy import select

from src.core.models.domain import Domain
from src.core.models.watch import ContentType, Watch
from src.dashboard.context import get_domains_with_watch_counts

pytestmark = pytest.mark.integration


class TestGetDomainsWithWatchCounts:
    async def test_empty_domains(self, db_session):
        result = await get_domains_with_watch_counts(db_session)
        assert result == []

    async def test_domain_with_watches(self, db_session):
        domain = Domain(name="example.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
            effective_domain="example.com",
        )
        db_session.add(watch)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert len(result) == 1
        assert result[0]["name"] == "example.com"
        assert result[0]["watch_count"] == 1
        assert result[0]["in_backoff"] is False

    async def test_domain_with_no_watches(self, db_session):
        domain = Domain(name="orphan.com", min_interval=1.0, max_concurrency=2)
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert len(result) == 1
        assert result[0]["watch_count"] == 0

    async def test_domain_in_backoff(self, db_session):
        domain = Domain(
            name="slow.com", min_interval=1.0, max_concurrency=2, current_interval=4.0
        )
        db_session.add(domain)
        await db_session.flush()

        result = await get_domains_with_watch_counts(db_session)
        assert result[0]["in_backoff"] is True
        assert result[0]["current_interval"] == 4.0
```

- [ ] **Step 7: Run dashboard context tests**

Run: `uv run pytest tests/dashboard/test_context.py -v`
Expected: ALL PASS

- [ ] **Step 8: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/dashboard/context.py src/dashboard/routes.py src/dashboard/templates/pages/domains.html src/dashboard/templates/partials/domains_table.html src/dashboard/templates/base.html tests/dashboard/
git commit -m "#32 feat: add dashboard domains page with watch counts and backoff status"
```

---

### Task 8: Final integration verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: clean

- [ ] **Step 3: Verify app starts**

Run: `export $(cat env | xargs) && timeout 5 uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 || true`
Expected: startup logs show "rate limiter hydrated" — no crash

- [ ] **Step 4: Verify migration state**

Run: `export $(cat env | xargs) && uv run alembic current`
Expected: shows head revision
