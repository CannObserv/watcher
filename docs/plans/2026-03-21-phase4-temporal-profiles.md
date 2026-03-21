# Phase 4: Temporal Watch Profiles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add temporal watch profiles — scheduling rules tied to external dates that dynamically adjust check frequency based on proximity to events, seasons, or deadlines, with configurable post-event actions.

**Architecture:** A `TemporalProfile` model linked to watches (one-to-many). A `resolve_profile` function evaluates active profiles against the current date and returns the effective check interval. `compute_next_check` is enhanced to consult temporal profiles before falling back to the base interval. API endpoints for CRUD on profiles. Post-event actions (reduce_frequency, deactivate, archive) applied by the scheduler.

**Tech Stack:** SQLAlchemy (model), Alembic (migration), FastAPI (API), existing scheduler.py

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  core/
    models/
      temporal_profile.py  — create: TemporalProfile model
      __init__.py           — modify: add TemporalProfile export
    scheduler.py            — modify: add resolve_profile, enhance compute_next_check
  api/
    schemas/
      temporal_profile.py   — create: Pydantic schemas for profile CRUD
    routes/
      temporal_profiles.py  — create: profile API endpoints (nested under watches)
    main.py                 — modify: include profiles router
alembic/
  versions/                 — new migration for temporal_profiles table
tests/
  core/
    test_scheduler.py       — modify: add temporal profile resolution tests
    test_models.py          — modify: add TemporalProfile model tests
  api/
    test_temporal_profiles.py — create: profile API endpoint tests
```

---

## Task 1: TemporalProfile model

**Files:**
- Create: `src/core/models/temporal_profile.py`
- Modify: `src/core/models/__init__.py`
- Modify: `tests/core/test_models.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/core/test_models.py`:

```python
from datetime import date
from src.core.models.temporal_profile import ProfileType, PostAction, TemporalProfile


class TestTemporalProfileModel:
    def test_create_event_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 15),
            rules=[
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
                {"days_before": 1, "interval": "15m"},
            ],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.profile_type == ProfileType.EVENT
        assert len(profile.rules) == 3
        assert profile.post_action == PostAction.REDUCE_FREQUENCY
        assert profile.reference_date == date(2026, 4, 15)

    def test_create_seasonal_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.SEASONAL,
            date_range_start=date(2026, 1, 15),
            date_range_end=date(2026, 6, 30),
            rules=[{"days_before": 0, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.profile_type == ProfileType.SEASONAL
        assert profile.date_range_start == date(2026, 1, 15)

    def test_create_deadline_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.DEADLINE,
            reference_date=date(2026, 5, 1),
            rules=[
                {"days_before": 14, "interval": "12h"},
                {"days_before": 3, "interval": "2h"},
            ],
            post_action=PostAction.DEACTIVATE,
        )
        assert profile.post_action == PostAction.DEACTIVATE

    def test_defaults(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 15),
            rules=[],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.is_active is True
        assert profile.date_range_start is None
        assert profile.date_range_end is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_models.py::TestTemporalProfileModel -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement TemporalProfile model**

Create `src/core/models/temporal_profile.py`:

```python
"""TemporalProfile model — scheduling rules tied to external dates."""

import enum
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class ProfileType(enum.StrEnum):
    """Type of temporal scheduling profile."""

    EVENT = "event"
    SEASONAL = "seasonal"
    DEADLINE = "deadline"


class PostAction(enum.StrEnum):
    """Action to take after the profile's date passes."""

    REDUCE_FREQUENCY = "reduce_frequency"
    DEACTIVATE = "deactivate"
    ARCHIVE = "archive"


class TemporalProfile(Base, TimestampMixin):
    """A temporal scheduling rule tied to a specific date or date range."""

    __tablename__ = "temporal_profiles"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id"))
    profile_type: Mapped[ProfileType] = mapped_column(String(20))
    reference_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_range_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_range_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    rules: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    post_action: Mapped[PostAction] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults."""
        kwargs.setdefault("rules", [])
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)
```

- [ ] **Step 4: Update models __init__.py**

Add `TemporalProfile`, `ProfileType`, `PostAction` to exports.

- [ ] **Step 5: Run tests, lint, commit**

```bash
git add src/core/models/ tests/core/test_models.py
git commit -m "#2 feat: add TemporalProfile model with ProfileType and PostAction enums"
```

---

## Task 2: Alembic migration

**Files:**
- New: `alembic/versions/<auto>_add_temporal_profiles.py`

- [ ] **Step 1: Generate migration**

```bash
export $(cat env | xargs)
uv run alembic revision --autogenerate -m "add temporal_profiles table"
```

- [ ] **Step 2: Apply migration**

```bash
uv run alembic upgrade head
```

- [ ] **Step 3: Verify**

```bash
PGPASSWORD=watcher psql -U watcher -d watcher -c "\d temporal_profiles"
```

- [ ] **Step 4: Commit**

```bash
git add alembic/
git commit -m "#2 feat: add migration for temporal_profiles table"
```

---

## Task 3: Temporal profile resolution in scheduler

This is the core logic. `resolve_effective_interval` takes a watch's profiles and returns the tightest (shortest) applicable interval.

**Files:**
- Modify: `src/core/scheduler.py`
- Modify: `tests/core/test_scheduler.py`

- [ ] **Step 1: Write failing tests for profile resolution**

Add to `tests/core/test_scheduler.py`:

```python
from datetime import date

from src.core.scheduler import resolve_effective_interval


class TestResolveEffectiveInterval:
    def test_no_profiles_returns_none(self):
        result = resolve_effective_interval(profiles=[], today=date(2026, 3, 21))
        assert result is None

    def test_event_profile_30_days_before(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
            ],
            "is_active": True,
        }]
        # 25 days before event — matches "30 days before" rule
        result = resolve_effective_interval(profiles, today=date(2026, 3, 21))
        assert result == timedelta(hours=6)

    def test_event_profile_7_days_before(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
            ],
            "is_active": True,
        }]
        # 5 days before event — matches "7 days before" rule (tighter)
        result = resolve_effective_interval(profiles, today=date(2026, 4, 10))
        assert result == timedelta(hours=1)

    def test_event_profile_after_event_returns_none(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "6h"}],
            "is_active": True,
        }]
        # After the event — profile doesn't apply
        result = resolve_effective_interval(profiles, today=date(2026, 4, 20))
        assert result is None

    def test_seasonal_within_range(self):
        profiles = [{
            "profile_type": "seasonal",
            "date_range_start": "2026-01-15",
            "date_range_end": "2026-06-30",
            "rules": [{"days_before": 0, "interval": "1h"}],
            "is_active": True,
        }]
        result = resolve_effective_interval(profiles, today=date(2026, 3, 21))
        assert result == timedelta(hours=1)

    def test_seasonal_outside_range_returns_none(self):
        profiles = [{
            "profile_type": "seasonal",
            "date_range_start": "2026-01-15",
            "date_range_end": "2026-06-30",
            "rules": [{"days_before": 0, "interval": "1h"}],
            "is_active": True,
        }]
        result = resolve_effective_interval(profiles, today=date(2026, 8, 1))
        assert result is None

    def test_deadline_approaching(self):
        profiles = [{
            "profile_type": "deadline",
            "reference_date": "2026-05-01",
            "rules": [
                {"days_before": 14, "interval": "12h"},
                {"days_before": 3, "interval": "2h"},
            ],
            "is_active": True,
        }]
        # 10 days before deadline — matches "14 days before"
        result = resolve_effective_interval(profiles, today=date(2026, 4, 21))
        assert result == timedelta(hours=12)

    def test_deadline_after_date_returns_none(self):
        profiles = [{
            "profile_type": "deadline",
            "reference_date": "2026-05-01",
            "rules": [{"days_before": 14, "interval": "12h"}],
            "is_active": True,
        }]
        result = resolve_effective_interval(profiles, today=date(2026, 5, 5))
        assert result is None

    def test_multiple_profiles_picks_shortest(self):
        profiles = [
            {
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 30, "interval": "6h"}],
                "is_active": True,
            },
            {
                "profile_type": "deadline",
                "reference_date": "2026-04-10",
                "rules": [{"days_before": 14, "interval": "2h"}],
                "is_active": True,
            },
        ]
        # Both apply; deadline gives 2h which is shorter
        result = resolve_effective_interval(profiles, today=date(2026, 3, 28))
        assert result == timedelta(hours=2)

    def test_inactive_profile_ignored(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "1h"}],
            "is_active": False,
        }]
        result = resolve_effective_interval(profiles, today=date(2026, 3, 21))
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_scheduler.py::TestResolveEffectiveInterval -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement resolve_effective_interval**

Add to `src/core/scheduler.py`:

```python
from datetime import date


def resolve_effective_interval(
    profiles: list[dict],
    today: date | None = None,
) -> timedelta | None:
    """Evaluate temporal profiles and return the shortest applicable interval.

    Profiles are dicts with keys: profile_type, reference_date (YYYY-MM-DD str or date),
    date_range_start, date_range_end, rules (list of {days_before, interval}), is_active.

    Returns None if no profile applies (caller should use the base interval).
    """
    today = today or date.today()
    candidates: list[timedelta] = []

    for profile in profiles:
        if not profile.get("is_active", True):
            continue

        interval = _resolve_single_profile(profile, today)
        if interval is not None:
            candidates.append(interval)

    if not candidates:
        return None
    return min(candidates)


def _resolve_single_profile(profile: dict, today: date) -> timedelta | None:
    """Resolve a single profile to an interval, or None if it doesn't apply today."""
    ptype = profile.get("profile_type")
    rules = profile.get("rules", [])

    if ptype == "event" or ptype == "deadline":
        return _resolve_date_profile(profile, today, rules)
    elif ptype == "seasonal":
        return _resolve_seasonal_profile(profile, today, rules)
    return None


def _resolve_date_profile(
    profile: dict, today: date, rules: list[dict]
) -> timedelta | None:
    """Resolve event or deadline profile — rules based on days_before reference_date."""
    ref = profile.get("reference_date")
    if ref is None:
        return None
    if isinstance(ref, str):
        ref = date.fromisoformat(ref)

    days_until = (ref - today).days
    if days_until < 0:
        return None  # Date has passed

    # Find the matching rule: the tightest rule whose days_before >= days_until
    # Rules are {days_before: N, interval: "Xh"} — match if days_until <= days_before
    matching = [r for r in rules if days_until <= r.get("days_before", 0)]
    if not matching:
        return None

    # Pick the rule with the smallest days_before (tightest window)
    best = min(matching, key=lambda r: r.get("days_before", 0))
    return parse_interval(best.get("interval"))


def _resolve_seasonal_profile(
    profile: dict, today: date, rules: list[dict]
) -> timedelta | None:
    """Resolve seasonal profile — active within date range."""
    start = profile.get("date_range_start")
    end = profile.get("date_range_end")
    if start is None or end is None:
        return None
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    if not (start <= today <= end):
        return None

    # Seasonal profiles use the first rule's interval (days_before=0 convention)
    if rules:
        return parse_interval(rules[0].get("interval"))
    return None
```

- [ ] **Step 4: Enhance compute_next_check to use profiles**

Modify `compute_next_check` signature and logic:

```python
def compute_next_check(
    schedule_config: dict,
    last_checked_at: datetime | None,
    now: datetime | None = None,
    profiles: list[dict] | None = None,
) -> datetime:
    """Compute when a watch should next be checked.

    If temporal profiles are provided, uses the shortest applicable profile
    interval. Falls back to schedule_config["interval"] if no profile applies.
    """
    now = now or datetime.now(UTC)
    today = now.date()

    # Resolve temporal profile interval (overrides base interval if applicable)
    profile_interval = resolve_effective_interval(profiles or [], today=today)
    interval = profile_interval or parse_interval(schedule_config.get("interval"))

    if last_checked_at is None:
        return now
    next_due = last_checked_at + interval
    if next_due <= now:
        return now
    return next_due
```

- [ ] **Step 5: Add test for compute_next_check with profiles**

```python
class TestComputeNextCheckWithProfiles:
    def test_profile_overrides_base_interval(self):
        now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        last = now - timedelta(hours=3)
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "2h"}],
            "is_active": True,
        }]
        result = compute_next_check(
            schedule_config={"interval": "1d"},
            last_checked_at=last,
            now=now,
            profiles=profiles,
        )
        # Profile gives 2h interval, so next = last + 2h = now - 1h → overdue → now
        assert result == now

    def test_no_profile_match_uses_base(self):
        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        last = now - timedelta(hours=12)
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "1h"}],
            "is_active": True,
        }]
        result = compute_next_check(
            schedule_config={"interval": "1d"},
            last_checked_at=last,
            now=now,
            profiles=profiles,
        )
        # Event is in the past, profile doesn't apply, uses 1d base
        expected = last + timedelta(days=1)
        assert result == expected
```

- [ ] **Step 6: Run tests, lint, commit**

```bash
git add src/core/scheduler.py tests/core/test_scheduler.py
git commit -m "#2 feat: add temporal profile resolution and integrate with scheduler"
```

---

## Task 4: Post-event actions

The scheduler should apply post-event actions when a profile's date has passed.

**Files:**
- Modify: `src/core/scheduler.py`
- Modify: `tests/core/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

```python
from src.core.scheduler import evaluate_post_actions


class TestEvaluatePostActions:
    def test_no_profiles_no_actions(self):
        actions = evaluate_post_actions(profiles=[], today=date(2026, 5, 1))
        assert actions == []

    def test_event_past_returns_action(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "1h"}],
            "post_action": "deactivate",
            "is_active": True,
        }]
        actions = evaluate_post_actions(profiles, today=date(2026, 4, 20))
        assert len(actions) == 1
        assert actions[0]["action"] == "deactivate"

    def test_event_not_yet_passed_no_action(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 30, "interval": "1h"}],
            "post_action": "deactivate",
            "is_active": True,
        }]
        actions = evaluate_post_actions(profiles, today=date(2026, 4, 10))
        assert actions == []

    def test_seasonal_past_end_returns_action(self):
        profiles = [{
            "profile_type": "seasonal",
            "date_range_start": "2026-01-15",
            "date_range_end": "2026-06-30",
            "rules": [{"days_before": 0, "interval": "1h"}],
            "post_action": "reduce_frequency",
            "is_active": True,
        }]
        actions = evaluate_post_actions(profiles, today=date(2026, 7, 5))
        assert len(actions) == 1
        assert actions[0]["action"] == "reduce_frequency"

    def test_inactive_profile_no_action(self):
        profiles = [{
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [],
            "post_action": "deactivate",
            "is_active": False,
        }]
        actions = evaluate_post_actions(profiles, today=date(2026, 5, 1))
        assert actions == []
```

- [ ] **Step 2: Implement evaluate_post_actions**

Add to `src/core/scheduler.py`:

```python
def evaluate_post_actions(
    profiles: list[dict],
    today: date | None = None,
) -> list[dict]:
    """Evaluate profiles for post-event actions that should be applied.

    Returns list of dicts with 'action' and 'profile' keys for profiles
    whose reference date or date range has passed.
    """
    today = today or date.today()
    actions = []

    for profile in profiles:
        if not profile.get("is_active", True):
            continue

        post_action = profile.get("post_action")
        if not post_action:
            continue

        ptype = profile.get("profile_type")
        is_past = False

        if ptype in ("event", "deadline"):
            ref = profile.get("reference_date")
            if ref:
                if isinstance(ref, str):
                    ref = date.fromisoformat(ref)
                is_past = today > ref

        elif ptype == "seasonal":
            end = profile.get("date_range_end")
            if end:
                if isinstance(end, str):
                    end = date.fromisoformat(end)
                is_past = today > end

        if is_past:
            actions.append({"action": post_action, "profile": profile})

    return actions
```

- [ ] **Step 3: Run tests, lint, commit**

```bash
git add src/core/scheduler.py tests/core/test_scheduler.py
git commit -m "#2 feat: add post-event action evaluation for temporal profiles"
```

---

## Task 5: Wire profiles into schedule_tick

**Files:**
- Modify: `src/workers/tasks.py`

- [ ] **Step 1: Update schedule_tick to load and use profiles**

Modify `schedule_tick` in `src/workers/tasks.py` to:
1. For each watch, load its active temporal profiles from DB
2. Pass profiles to `compute_next_check`
3. After checking, evaluate post-actions and apply them (deactivate watch, mark profile inactive)

```python
# In schedule_tick, after loading watches:
from src.core.models.temporal_profile import TemporalProfile
from src.core.scheduler import evaluate_post_actions

# For each watch, load its profiles
for watch in watches:
    # Load active profiles for this watch
    profile_stmt = select(TemporalProfile).where(
        TemporalProfile.watch_id == watch.id,
        TemporalProfile.is_active.is_(True),
    )
    profile_result = await session.execute(profile_stmt)
    db_profiles = list(profile_result.scalars().all())

    # Convert to dicts for scheduler functions
    profiles = [
        {
            "profile_type": p.profile_type,
            "reference_date": p.reference_date,
            "date_range_start": p.date_range_start,
            "date_range_end": p.date_range_end,
            "rules": p.rules,
            "post_action": p.post_action,
            "is_active": p.is_active,
            "id": str(p.id),
        }
        for p in db_profiles
    ]

    # Check for post-event actions
    post_actions = evaluate_post_actions(profiles, today=now.date())
    for pa in post_actions:
        action = pa["action"]
        if action == "deactivate":
            watch.is_active = False
            logger.info("deactivated watch (post-event)", extra={"watch_id": str(watch.id)})
        elif action == "reduce_frequency":
            # Reset to daily default
            watch.schedule_config = {**watch.schedule_config, "interval": "1d"}
            logger.info("reduced frequency (post-event)", extra={"watch_id": str(watch.id)})
        # Mark the profile as inactive so it doesn't re-trigger
        profile_id = pa["profile"].get("id")
        if profile_id:
            for p in db_profiles:
                if str(p.id) == profile_id:
                    p.is_active = False

    # Compute next check with profile awareness
    next_due = compute_next_check(
        schedule_config=watch.schedule_config or {},
        last_checked_at=watch.last_checked_at,
        now=now,
        profiles=profiles,
    )
    if next_due <= now and watch.is_active:
        await check_watch.configure().defer_async(watch_id=str(watch.id))
        deferred += 1

# Commit any post-action changes
await session.commit()
```

NOTE: The query needs to happen inside the session context. The implementer will need to restructure `schedule_tick` so the watch iteration happens inside the `async with session` block, since we now need to load profiles and commit post-action changes.

- [ ] **Step 2: Run full test suite to verify no regressions**

```bash
uv run pytest -v
uv run pytest -m integration -v
```

- [ ] **Step 3: Commit**

```bash
git add src/workers/tasks.py
git commit -m "#2 feat: wire temporal profiles into schedule_tick with post-event actions"
```

---

## Task 6: Temporal profile API endpoints

**Files:**
- Create: `src/api/schemas/temporal_profile.py`
- Create: `src/api/routes/temporal_profiles.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_temporal_profiles.py`

- [ ] **Step 1: Write failing tests**

Create `tests/api/test_temporal_profiles.py`:

```python
"""Integration tests for temporal profile API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestCreateProfile:
    async def test_create_event_profile(self, client):
        # First create a watch
        watch_resp = await client.post("/api/watches", json={
            "name": "Profiled Watch",
            "url": "https://example.com",
            "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]

        response = await client.post(f"/api/watches/{watch_id}/profiles", json={
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [
                {"days_before": 30, "interval": "6h"},
                {"days_before": 7, "interval": "1h"},
            ],
            "post_action": "reduce_frequency",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["profile_type"] == "event"
        assert data["reference_date"] == "2026-04-15"
        assert len(data["rules"]) == 2

    async def test_create_profile_invalid_watch(self, client):
        response = await client.post(
            "/api/watches/00000000000000000000000000/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        assert response.status_code == 404


class TestListProfiles:
    async def test_list_profiles_for_watch(self, client):
        watch_resp = await client.post("/api/watches", json={
            "name": "Multi-Profile Watch",
            "url": "https://example.com",
            "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]

        await client.post(f"/api/watches/{watch_id}/profiles", json={
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [{"days_before": 7, "interval": "1h"}],
            "post_action": "deactivate",
        })
        await client.post(f"/api/watches/{watch_id}/profiles", json={
            "profile_type": "seasonal",
            "date_range_start": "2026-01-15",
            "date_range_end": "2026-06-30",
            "rules": [{"days_before": 0, "interval": "2h"}],
            "post_action": "reduce_frequency",
        })

        response = await client.get(f"/api/watches/{watch_id}/profiles")
        assert response.status_code == 200
        assert len(response.json()) == 2


class TestDeleteProfile:
    async def test_delete_profile(self, client):
        watch_resp = await client.post("/api/watches", json={
            "name": "Delete Profile Watch",
            "url": "https://example.com",
            "content_type": "html",
        })
        watch_id = watch_resp.json()["id"]

        create_resp = await client.post(f"/api/watches/{watch_id}/profiles", json={
            "profile_type": "event",
            "reference_date": "2026-04-15",
            "rules": [],
            "post_action": "deactivate",
        })
        profile_id = create_resp.json()["id"]

        response = await client.delete(
            f"/api/watches/{watch_id}/profiles/{profile_id}"
        )
        assert response.status_code == 204
```

- [ ] **Step 2: Implement schemas**

Create `src/api/schemas/temporal_profile.py`:

```python
"""Pydantic schemas for temporal profile CRUD."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from src.core.models.temporal_profile import PostAction, ProfileType


class ProfileRuleItem(BaseModel):
    """A single rule: {days_before, interval}."""

    days_before: int
    interval: str


class ProfileCreate(BaseModel):
    """Schema for creating a temporal profile."""

    profile_type: ProfileType
    reference_date: date | None = None
    date_range_start: date | None = None
    date_range_end: date | None = None
    rules: list[ProfileRuleItem] = Field(default_factory=list)
    post_action: PostAction


class ProfileResponse(BaseModel):
    """Schema for returning a temporal profile."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    watch_id: str
    profile_type: ProfileType
    reference_date: date | None
    date_range_start: date | None
    date_range_end: date | None
    rules: list
    post_action: PostAction
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 3: Implement routes**

Create `src/api/routes/temporal_profiles.py`:

```python
"""Temporal profile API endpoints — nested under watches."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.temporal_profile import ProfileCreate, ProfileResponse
from src.core.models.audit_log import AuditLog
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/watches/{watch_id}/profiles", tags=["temporal-profiles"])


def _parse_ulid(value: str) -> ULID:
    try:
        return ULID.from_str(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


@router.post("", status_code=201, response_model=ProfileResponse)
async def create_profile(
    watch_id: str,
    data: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a temporal profile for a watch."""
    watch = await session.get(Watch, _parse_ulid(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    profile = TemporalProfile(
        watch_id=watch.id,
        profile_type=data.profile_type,
        reference_date=data.reference_date,
        date_range_start=data.date_range_start,
        date_range_end=data.date_range_end,
        rules=[r.model_dump() for r in data.rules],
        post_action=data.post_action,
    )
    session.add(profile)
    session.add(AuditLog(
        event_type="profile.created",
        watch_id=watch.id,
        payload={"profile_type": data.profile_type, "post_action": data.post_action},
    ))
    await session.commit()
    await session.refresh(profile)
    return profile


@router.get("", response_model=list[ProfileResponse])
async def list_profiles(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """List all temporal profiles for a watch."""
    stmt = select(TemporalProfile).where(
        TemporalProfile.watch_id == _parse_ulid(watch_id)
    ).order_by(TemporalProfile.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    watch_id: str,
    profile_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a temporal profile."""
    profile = await session.get(TemporalProfile, _parse_ulid(profile_id))
    if not profile or profile.watch_id != _parse_ulid(watch_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    await session.delete(profile)
    session.add(AuditLog(
        event_type="profile.deleted",
        watch_id=profile.watch_id,
        payload={"profile_id": str(profile.id)},
    ))
    await session.commit()
```

- [ ] **Step 4: Update main.py to include profiles router**

```python
from src.api.routes.temporal_profiles import router as profiles_router
app.include_router(profiles_router)
```

- [ ] **Step 5: Run integration tests**

```bash
uv run pytest tests/api/test_temporal_profiles.py -v -m integration
```

- [ ] **Step 6: Commit**

```bash
git add src/api/ tests/api/test_temporal_profiles.py
git commit -m "#2 feat: add temporal profile API (create, list, delete)"
```

---

## Task 7: Update documentation

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update AGENTS.md**

Add to project layout:
```
src/core/models/temporal_profile.py — Temporal scheduling profiles (event, seasonal, deadline)
```

- [ ] **Step 2: Run full suite, lint, commit**

```bash
uv run pytest -v
uv run pytest -m integration -v
uv run ruff check .
git add AGENTS.md
git commit -m "#2 docs: add temporal profiles to project layout"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | TemporalProfile model | ~4 unit |
| 2 | Alembic migration | manual verify |
| 3 | Profile resolution + scheduler integration | ~12 unit |
| 4 | Post-event action evaluation | ~5 unit |
| 5 | Wire profiles into schedule_tick | regression check |
| 6 | Temporal profile API (create, list, delete) | ~4 integration |
| 7 | Documentation | — |

Total: ~25 new automated tests
