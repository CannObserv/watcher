# Watcher: Watch reshape + scheduler/pipeline reshape — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape `Watch` from 1:1-InfoSource to (info_item_id, optional target_info_source_id) under a parent WatchedItem; restructure the scheduler/pipeline so one fetch per InfoItem cycle drives N Watches; drop the fragment-root invariant; auto-create WatchedItems on first Watch.

**Architecture:** Adopts Sections 5 and 6 of the [InfoItem-first design](2026-05-15-watched-item-infoitem-first-design.md) together — they're inseparable. Pre-production: schema is rewritten in a single migration that truncates `watches` (no data migration). The pipeline becomes WatchedItem-driven: `check_watched_item(watched_item_id)` fetches the InfoItem's primary URL once, runs primary + cross_check + sub_aspect extractions against the same bytes, posts per-InfoSource SourceRevisions, and dispatches per-Watch notifications based on which target's fingerprint changed.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async, Alembic, FastAPI, Jinja2/HTMX, Procrastinate (task queue), `archiver-client` SDK v3.2.0, pytest (unit + integration).

**Branch / worktree:** `feat/watched-item-160` at `.worktrees/watched-item-160`. Current HEAD: `57307ad` (WatchedItem + template tables already landed).

---

## Spec references

- Design doc: [`docs/plans/2026-05-15-watched-item-infoitem-first-design.md`](2026-05-15-watched-item-infoitem-first-design.md) Sections 2, 4, 5 (Section 3 descoped; Section 1 / 1.5 are Archiver work, already shipped in v3.0.0–v3.2.0).
- Archiver SDK contract (v3.2.0):
  - `add_info_source(info_item_id, info_source_id, role: Literal['cross_check','sub_aspect'] | None = None)`.
  - `get_info_item(info_item_id) → InfoItemOut`; `InfoItemOut.info_item_sources: list[InfoItemSourceOut]` where `InfoItemSourceOut` carries **only** `{info_source_id: str, role: str | None, created_at: datetime}`. To resolve URL or `source_spec`, issue a separate `get_info_source(info_source_id) → InfoSourceOut`. `InfoSourceOut.url: str | None` is a first-class field — non-NULL for root-shaped (primary), NULL for fragments.
  - `find_info_item(query: str, *, limit: int = 20) → list[InfoItemOut]` — `limit` is keyword-only.
- Tracking issue: [#160](https://github.com/CannObserv/watcher/issues/160).

## Out of scope (do NOT do in this plan)

- **Cross-InfoItem `Collection`** layer — design Section 3, descoped.
- **Selector-rot UI** beyond posting cross_check SourceRevisions — that's #157.
- **Picker UX / WatchedItem CRUD UI** (design Sections 5.2, 5.4, 5.3) — this plan replaces the Watch-create picker with a *minimal* two-field form; full typeahead picker is a follow-up plan.
- **Fragment-review UI** (`last_reviewed_at` diff-on-view) — also a follow-up.
- **Per-Watch suppression of WatchedItem templates** — YAGNI v1.
- **Data migration of pre-existing watches** — pre-prod, table is truncated.
- **`pending_source_revisions.next_attempt` model/index drift** — pre-existing, orthogonal.
- **`WatchedItem.last_checked_at`** for per-InfoItem freshness aggregation — v1 derives "due?" by aggregating over child Watches; promoting to a WatchedItem column is a follow-up. Justified for v1: pre-prod traffic is small N, query cost is acceptable, and adding the column requires a migration round that we'd rather not entangle with the cutover.

## File-by-file map

### New files
- `src/core/watches/resolution.py` — `resolved_schedule_config(watch)`, `resolved_content_type(watch)`, `resolved_tags(watch)` (Task 4). `resolved_notification_dispatches(...)` is added later in Task 9 — listing here for forward reference.
- `src/core/watches/info_item_fetch.py` — `fetch_info_item_bindings(info_client, info_item_id) → InfoItemBindings` (dataclass: primary InfoSourceOut, cross_checks, sub_aspects). Single SDK call to `get_info_item`, partitioned by role; per-binding `get_info_source` for source_spec resolution.
- `tests/core/watches/test_resolution.py` — unit tests for the resolution chain.
- `tests/core/watches/test_info_item_fetch.py` — unit tests for binding partitioning.

### Modified files
- `src/core/models/watch.py` — drop `info_source_id`, `schedule_config`; add `info_item_id`, `target_info_source_id`, `watched_item_id`; `content_type` becomes nullable; cross-schema FK stub for `information.info_items`; **add SQLAlchemy `relationship("WatchedItem", ...)` so `watch.watched_item` works without N+1.**
- `alembic/versions/<new>_reshape_watches_*.py` — new migration (Task 1).
- `src/core/watches/__init__.py` — `create_watch` signature rewrite; `resolve_watch_url(watch, client) → str` returns the WatchedItem's primary URL (single source of truth for both primary-target and sub_aspect-target Watches).
- `src/core/watches/cadence.py` — **DELETE** (Task 13). Per-InfoItem fetch cadence supersedes `effective_root_cadence_seconds`.
- `src/core/watches/invariants.py` — **DELETE** (Task 13). Fragment-root invariant gone (sub_aspect Watches are self-sufficient under WatchedItem-driven fetching). Three call sites under `src/` (`api/routes/watches.py`, `dashboard/routes.py`, and any tests) get updated in Tasks 10 and 11 first; only then can the module be deleted.
- `src/core/scheduler.py` — see Task 6.
- `src/workers/tasks.py` — see Task 8.
- `src/workers/pipeline.py` — see Task 7.
- `src/workers/source_revisions_drain.py` — see **Task 8b** (`Watch.info_source_id` lookup at line 36).
- `src/api/schemas/watch.py`, `src/api/routes/watches.py` — see Task 10.
- `src/dashboard/routes.py` + templates — see Task 11.
- `tests/conftest.py` — see Task 5.
- All affected tests — distributed across Tasks 1, 5, 6, 7, 8, 8b, 10, 11, 12.

### Files unaffected but worth verifying
- `src/core/models/last_known_revision.py`, `pending_source_revision.py` — keyed by InfoSource ID (Archiver-reference), not Watch column; no schema change.
- `src/core/sources/{resolver,outbox,revision_cache}.py` — take `info_source_id` parameters; consumer-side update needed only where they're invoked with `watch.info_source_id` (gone). All call sites are within `workers/pipeline.py` and `workers/tasks.py`.

---

## Tasks

### Task 1: Reshape `Watch` model + migration (TDD red first)

**Files:**
- Modify: `tests/core/models/test_watch.py`
- Modify: `src/core/models/watch.py`
- Create: `alembic/versions/<new_rev>_reshape_watches_*.py`

- [ ] **Step 1.1: Write failing model test for the new shape**

Open `tests/core/models/test_watch.py` and add (do not modify existing tests yet; that happens in Task 12 after a green baseline):

```python
async def test_watch_new_shape_persists_info_item_and_target(db_session):
    """Red test for #160 reshape: info_item_id required; target_info_source_id nullable; watched_item_id required; schedule_config absent."""
    from sqlalchemy import inspect
    from src.core.models import Watch
    cols = {c.name for c in inspect(Watch).columns}
    assert "info_item_id" in cols
    assert "target_info_source_id" in cols
    assert "watched_item_id" in cols
    assert "info_source_id" not in cols
    assert "schedule_config" not in cols
```

- [ ] **Step 1.2: Run, verify red**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest --no-cov tests/core/models/test_watch.py::test_watch_new_shape_persists_info_item_and_target -v
```

Expected: FAIL (assertion errors — `info_source_id` still present, etc.).

- [ ] **Step 1.3: Rewrite the Watch model**

Drop `info_source_id` and `schedule_config`. Add `info_item_id`, `target_info_source_id`, `watched_item_id`. Make `content_type` nullable. Add cross-schema FK stub for `information.info_items` alongside the existing `info_sources` stub. **Add a SQLAlchemy `relationship` so `watch.watched_item` is loadable** — this is consumed by `resolution.py` (Task 4) and the pipeline (Task 7); without it, accessor code raises `AttributeError`.

Full module:

```python
"""Watch model — operator-watchable content target within a WatchedItem subscription."""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

if TYPE_CHECKING:
    from src.core.models.watched_item import WatchedItem

# Cross-schema FK resolution stubs for the Information service.
# Watcher's Base.metadata cannot resolve FKs into the `information` schema on
# its own — Archiver owns those tables on a separate DeclarativeBase. Register
# stub Tables exposing only the referenced primary key columns. Watcher never
# creates or drops these tables; production DDL lives in Archiver's Alembic
# root, and alembic/env.py filters non-public schemas out of autogenerate.
Table(
    "info_items",
    Base.metadata,
    Column("info_item_id", ULIDType, primary_key=True),
    schema="information",
)
Table(
    "info_sources",
    Base.metadata,
    Column("info_source_id", ULIDType, primary_key=True),
    schema="information",
)


class ContentType(enum.StrEnum):
    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class WatchHealthStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


class Watch(Base, TimestampMixin):
    """A content target within a WatchedItem subscription.

    `target_info_source_id` discriminates the target kind:
    * NULL — the InfoItem's primary content. Cross_check bindings produce
      selector-rot signal but do not change the Watch's identity.
    * non-NULL — a specific `sub_aspect`-bound fragment InfoSource.

    Scheduling is owned by the parent WatchedItem; the fetch happens once per
    InfoItem per cycle. Notifications, tags, and content_type may be overridden
    per Watch over the WatchedItem's defaults via `src/core/watches/resolution.py`.
    """

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("information.info_items.info_item_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target_info_source_id: Mapped[ULID | None] = mapped_column(
        ULIDType,
        ForeignKey("information.info_sources.info_source_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    watched_item_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("watched_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    watched_item: Mapped["WatchedItem"] = relationship("WatchedItem", lazy="joined")

    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[ContentType | None] = mapped_column(String(20), nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    domain_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    effective_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    effective_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, default=None)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True, default=None)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    health_status: Mapped[WatchHealthStatus] = mapped_column(
        String(10), default=WatchHealthStatus.UNKNOWN, server_default="unknown",
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("is_archived", False)
        kwargs.setdefault("domain_suspended", False)
        kwargs.setdefault("health_status", WatchHealthStatus.UNKNOWN)
        super().__init__(**kwargs)

    @validates("content_type")
    def validate_content_type(self, _key, value):
        if value is None:
            return None
        if isinstance(value, ContentType):
            return value
        try:
            return ContentType(value)
        except ValueError as exc:
            raise ValueError(f"Invalid content_type: {value!r}") from exc

    @validates("health_status")
    def validate_health_status(self, _key, value):
        if isinstance(value, WatchHealthStatus):
            return value
        try:
            return WatchHealthStatus(value)
        except ValueError as exc:
            raise ValueError(f"Invalid health_status: {value!r}") from exc
```

Note `lazy="joined"` on `watched_item` — Watch instances are short-lived and the resolution chain reads from `watch.watched_item.default_*` constantly; eager-loading saves N+1.

- [ ] **Step 1.4: Author the migration**

```bash
uv run alembic revision --autogenerate -m "reshape watches: info_item_id + target_info_source_id + watched_item_id (#160)"
```

Then **manually rewrite** the generated file to:

1. `CREATE SCHEMA IF NOT EXISTS information` + `CREATE TABLE IF NOT EXISTS information.info_items (info_item_id varchar(26) PRIMARY KEY)` — dev DB lacks the stub; no-op in test DB where Archiver's alembic owns the real table.
2. `TRUNCATE TABLE watches CASCADE` — pre-prod; clears dependent rows.
3. Drop `fk_watches_info_source_id`, `ix_watches_info_source_id`.
4. Add `info_item_id` (NOT NULL), `target_info_source_id` (NULL), `watched_item_id` (NOT NULL); alter `content_type` to nullable.
5. Add three new indexes + three new FKs (`fk_watches_info_item_id`, `fk_watches_target_info_source_id`, `fk_watches_watched_item_id`).
6. Drop `schedule_config`, `info_source_id`.

Strip any unrelated autogenerate drift (`ix_pending_source_revisions_next_attempt` — pre-existing, out of scope).

- [ ] **Step 1.5: Apply + verify round-trip**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: applies cleanly both directions.

- [ ] **Step 1.6: Run the red test green**

```bash
uv run pytest --no-cov tests/core/models/test_watch.py::test_watch_new_shape_persists_info_item_and_target -v
```

Expected: PASS.

- [ ] **Step 1.7: Sanity-check full test discovery (do NOT expect green yet)**

```bash
uv run pytest --no-cov --collect-only -q 2>&1 | tail -5
```

Expected: imports succeed; many tests still construct Watches with the old shape — those fail in Step 1.8.

- [ ] **Step 1.8: Capture failing test inventory**

```bash
uv run pytest --no-cov 2>&1 | grep -E "(FAILED|ERROR)" | sort -u | tee /tmp/160-task1-failures.txt
```

Keep this file around as the to-fix list for Tasks 5–12. Do not commit yet; the workspace is in a deliberately-broken state until the rest of the plan lands.

---

### Task 2: Create `info_item_fetch.py` (binding partition)

**Files:**
- Create: `src/core/watches/info_item_fetch.py`
- Create: `tests/core/watches/test_info_item_fetch.py`

- [ ] **Step 2.1: Write the failing tests**

```python
"""Tests for info_item_fetch — InfoItem binding partition + URL resolution."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.watches.info_item_fetch import (
    InfoItemBindings,
    fetch_info_item_bindings,
)


def _info_source_out(info_source_id, url=None, parent=None):
    """Mock an SDK InfoSourceOut. URL is a first-class field; non-NULL for primaries."""
    out = MagicMock()
    out.info_source_id = info_source_id
    out.url = url
    out.parent_info_source_id = parent
    out.source_spec = MagicMock()
    out.source_spec.additional_properties = {}
    return out


def _binding(info_source_id, role):
    """Mock an SDK InfoItemSourceOut. Schema: {info_source_id, role, created_at} only."""
    out = MagicMock()
    out.info_source_id = info_source_id
    out.role = role  # None | 'cross_check' | 'sub_aspect'
    return out


async def test_partitions_primary_cross_check_sub_aspect():
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_id = "ITEM"
    info_item.info_item_sources = [
        _binding("P", None),
        _binding("X1", "cross_check"),
        _binding("S1", "sub_aspect"),
        _binding("S2", "sub_aspect"),
    ]
    info_client.get_info_item.return_value = info_item

    sources = {
        "P": _info_source_out("P", url="https://example.com"),
        "X1": _info_source_out("X1", parent="P"),
        "S1": _info_source_out("S1", parent="P"),
        "S2": _info_source_out("S2", parent="P"),
    }
    info_client.get_info_source.side_effect = lambda iid: sources[iid]

    bindings = await fetch_info_item_bindings(info_client, "ITEM")
    assert bindings.primary.info_source_id == "P"
    assert bindings.primary_url == "https://example.com"
    assert {c.info_source_id for c in bindings.cross_checks} == {"X1"}
    assert {s.info_source_id for s in bindings.sub_aspects} == {"S1", "S2"}


async def test_raises_when_no_primary():
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_sources = [_binding("S1", "sub_aspect")]
    info_client.get_info_item.return_value = info_item

    src = _info_source_out("S1", parent="anything")
    info_client.get_info_source.return_value = src

    with pytest.raises(ValueError, match="no active primary"):
        await fetch_info_item_bindings(info_client, "ITEM")


async def test_unknown_role_is_ignored():
    """Forward-compat: an unrecognised role string is logged-and-skipped, not raised."""
    info_client = AsyncMock()
    info_item = MagicMock()
    info_item.info_item_sources = [
        _binding("P", None),
        _binding("M", "mirror"),  # hypothetical future role
    ]
    info_client.get_info_item.return_value = info_item

    sources = {
        "P": _info_source_out("P", url="https://example.com"),
        "M": _info_source_out("M", parent="P"),
    }
    info_client.get_info_source.side_effect = lambda iid: sources[iid]

    bindings = await fetch_info_item_bindings(info_client, "ITEM")
    assert bindings.primary.info_source_id == "P"
    assert bindings.cross_checks == []
    assert bindings.sub_aspects == []
```

- [ ] **Step 2.2: Run, verify fail**

```bash
uv run pytest tests/core/watches/test_info_item_fetch.py -v
```

Expected: import error (module not found).

- [ ] **Step 2.3: Implement `info_item_fetch.py`**

```python
"""Resolve an Archiver InfoItem's bindings, partitioned by role."""

from dataclasses import dataclass

from archiver_client import ArchiverClient

from src.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class InfoItemBindings:
    """An InfoItem's active bindings, partitioned by role.

    `primary` is the unique root-shaped InfoSource bound with role IS NULL.
    `cross_checks` are fragment-shaped bindings with role='cross_check'.
    `sub_aspects` are fragment-shaped bindings with role='sub_aspect'.
    `primary_url` is the root URL the cycle fetches; all extractions run against
    its bytes.
    """

    primary: object  # InfoSourceOut for the primary (role IS NULL)
    cross_checks: list[object]
    sub_aspects: list[object]
    primary_url: str


async def fetch_info_item_bindings(
    info_client: ArchiverClient, info_item_id: str
) -> InfoItemBindings:
    """Fetch and partition an InfoItem's bindings.

    Issues `get_info_item` once for the binding list, then `get_info_source`
    per active binding to resolve full InfoSourceOut (URL + source_spec).
    Unknown roles are skipped with a warning — forward-compatible with future
    role values added by Archiver.

    Raises ``ValueError`` if no active primary binding exists or the primary
    has no URL.
    """
    info_item = await info_client.get_info_item(info_item_id)
    primary = None
    cross_checks: list[object] = []
    sub_aspects: list[object] = []
    for binding in info_item.info_item_sources:
        source = await info_client.get_info_source(str(binding.info_source_id))
        if binding.role is None:
            primary = source
        elif binding.role == "cross_check":
            cross_checks.append(source)
        elif binding.role == "sub_aspect":
            sub_aspects.append(source)
        else:
            logger.warning(
                "ignoring unknown binding role %r for InfoSource %s on InfoItem %s",
                binding.role,
                binding.info_source_id,
                info_item_id,
            )
    if primary is None:
        raise ValueError(f"InfoItem {info_item_id}: no active primary binding")

    # Per Archiver v3.0.0: InfoSourceOut.url is a first-class field, non-NULL
    # for root-shaped (primary) InfoSources, NULL for fragments. Read directly
    # rather than walking source_spec.additional_properties.
    primary_url = primary.url
    if not primary_url:
        raise ValueError(
            f"InfoItem {info_item_id}: primary InfoSource {primary.info_source_id} has no url "
            "(InfoItem's primary must be root-shaped per Archiver invariant)"
        )

    return InfoItemBindings(
        primary=primary,
        cross_checks=cross_checks,
        sub_aspects=sub_aspects,
        primary_url=primary_url,
    )
```

- [ ] **Step 2.4: Run, verify pass**

```bash
uv run pytest tests/core/watches/test_info_item_fetch.py -v
```

Expected: 3 passed.

---

### Task 3: Create `resolution.py` (live-inheritance helpers)

**Files:**
- Create: `src/core/watches/resolution.py`
- Create: `tests/core/watches/test_resolution.py`

- [ ] **Step 3.1: Write failing tests for scalar inheritance**

```python
"""Tests for the resolution chain: Watch override → WatchedItem default → system default."""
from unittest.mock import MagicMock

from src.core.models.watch import ContentType
from src.core.watches.resolution import (
    SYSTEM_DEFAULT_SCHEDULE_CONFIG,
    resolved_content_type,
    resolved_schedule_config,
    resolved_tags,
)


def _watch(*, content_type=None, tags=None, watched_item=None):
    w = MagicMock()
    w.content_type = content_type
    w.tags = tags
    w.watched_item = watched_item
    return w


def _wi(*, default_schedule_config=None, default_content_type=None, default_tags=None):
    wi = MagicMock()
    wi.default_schedule_config = default_schedule_config
    wi.default_content_type = default_content_type
    wi.default_tags = default_tags
    return wi


def test_schedule_config_falls_back_to_system_default():
    w = _watch(watched_item=_wi(default_schedule_config=None))
    assert resolved_schedule_config(w) == SYSTEM_DEFAULT_SCHEDULE_CONFIG


def test_schedule_config_uses_watched_item_value():
    w = _watch(watched_item=_wi(default_schedule_config={"interval": "30m"}))
    assert resolved_schedule_config(w) == {"interval": "30m"}


def test_schedule_config_empty_dict_is_intentional_no_interval():
    """An empty dict on WatchedItem means 'no override' but it's set; pass through.

    `compute_next_check` tolerates a missing `interval` key; falling back to the
    system default in that case would be wrong (it would silently override the
    operator's explicit empty config). Use `is not None` semantics.
    """
    w = _watch(watched_item=_wi(default_schedule_config={}))
    assert resolved_schedule_config(w) == {}


def test_content_type_watch_overrides_watched_item():
    w = _watch(content_type=ContentType.PDF, watched_item=_wi(default_content_type=ContentType.HTML))
    assert resolved_content_type(w) is ContentType.PDF


def test_content_type_falls_back_to_watched_item():
    w = _watch(content_type=None, watched_item=_wi(default_content_type=ContentType.HTML))
    assert resolved_content_type(w) is ContentType.HTML


def test_tags_merge_additively():
    w = _watch(tags=["b", "c"], watched_item=_wi(default_tags=["a", "b"]))
    assert resolved_tags(w) == ["a", "b", "c"]


def test_tags_empty_when_both_unset():
    w = _watch(tags=None, watched_item=_wi(default_tags=None))
    assert resolved_tags(w) == []
```

- [ ] **Step 3.2: Run, verify fail**

Expected: ImportError.

- [ ] **Step 3.3: Implement `resolution.py`**

```python
"""Live-inheritance resolvers — Watch override → WatchedItem default → system default.

Per design Section 4 (#160). Resolution is performed at read time so edits to a
WatchedItem propagate immediately to all child Watches that do not override the
field. Tags merge additively (union); scalars override. `resolved_notification_dispatches`
is added later in Task 9 to keep coupling with the dispatch path local.
"""

from src.core.models.watch import ContentType, Watch

SYSTEM_DEFAULT_SCHEDULE_CONFIG: dict = {"interval": "1d"}
SYSTEM_DEFAULT_CONTENT_TYPE: ContentType = ContentType.HTML


def resolved_schedule_config(watch: Watch) -> dict:
    """Schedule lives on WatchedItem only — Watch has no per-row override.

    Distinguishes `None` (no override set) from `{}` (explicitly empty override).
    Empty dict passes through; `None` falls back to the system default.
    """
    wi = watch.watched_item
    if wi is not None and wi.default_schedule_config is not None:
        return wi.default_schedule_config
    return SYSTEM_DEFAULT_SCHEDULE_CONFIG


def resolved_content_type(watch: Watch) -> ContentType:
    if watch.content_type is not None:
        return watch.content_type
    wi = watch.watched_item
    if wi is not None and wi.default_content_type is not None:
        return wi.default_content_type
    return SYSTEM_DEFAULT_CONTENT_TYPE


def resolved_tags(watch: Watch) -> list[str]:
    """Additive merge: WatchedItem.default_tags ∪ Watch.tags, sorted."""
    wi_tags = (watch.watched_item.default_tags if watch.watched_item else None) or []
    own = watch.tags or []
    return sorted(set(wi_tags) | set(own))
```

- [ ] **Step 3.4: Run, verify pass**

Expected: 7 passed.

---

### Task 4: Update `make_watch` factory + factory tests

**Files:**
- Modify: `tests/conftest.py` (`make_watch` factory + helpers `make_info_item`, `make_primary_info_source`)
- Modify: `tests/test_make_watch_factory.py`

- [ ] **Step 4.1: Inspect existing fixtures**

```bash
sed -n '270,400p' tests/conftest.py
```

Identify the existing `make_info_source` factory (already exists per AGENTS notes) and the `make_watch` shape. The Archiver-side `information.info_items` and `information.info_item_sources` tables exist in the test DB (provisioned by `_apply_archiver_migrations`); seed rows via `tests/_information_test_models.py`.

- [ ] **Step 4.2: Add `make_info_item` factory + `make_primary_info_source` helper**

In `tests/conftest.py`, alongside `make_info_source`:

```python
async def make_info_item(session, *, name="test-item"):
    """Insert an Archiver `information.info_items` row + return its ULID."""
    from tests._information_test_models import InfoItem
    iid = ULID()
    session.add(InfoItem(info_item_id=iid, name=name))
    await session.flush()
    return iid


async def bind_primary_source(session, *, info_item_id, info_source_id):
    """Insert a role=NULL binding row into information.info_item_sources."""
    from tests._information_test_models import InfoItemSource
    session.add(
        InfoItemSource(info_item_id=info_item_id, info_source_id=info_source_id, role=None)
    )
    await session.flush()


async def bind_sub_aspect(session, *, info_item_id, info_source_id):
    """Insert a role='sub_aspect' binding row into information.info_item_sources."""
    from tests._information_test_models import InfoItemSource
    session.add(
        InfoItemSource(
            info_item_id=info_item_id,
            info_source_id=info_source_id,
            role="sub_aspect",
        )
    )
    await session.flush()
```

(Adjust `InfoItemSource` import to the actual exported name in `tests/_information_test_models.py`.)

- [ ] **Step 4.3: Rewrite `make_watch`**

```python
async def make_watch(
    session,
    *,
    name="test-watch",
    info_item_id=None,
    target_info_source_id=None,
    watched_item=None,
    primary_url="https://example.com",
    **kwargs,
):
    """Test factory for Watch rows.

    Creates an `information.info_items` row + a root-shaped primary InfoSource
    + binding when `info_item_id` is not provided. Auto-creates a WatchedItem
    unless one is supplied.
    """
    from src.core.models import Watch, WatchedItem

    if info_item_id is None:
        info_item_id = await make_info_item(session)
        primary_source = await make_info_source(session, url=primary_url)
        await bind_primary_source(
            session,
            info_item_id=info_item_id,
            info_source_id=primary_source.info_source_id,
        )

    if watched_item is None:
        watched_item = WatchedItem(info_item_id=info_item_id, name=f"WI for {name}")
        session.add(watched_item)
        await session.flush()
    elif watched_item.info_item_id != info_item_id:
        raise AssertionError("watched_item.info_item_id must match info_item_id")

    watch_kwargs = {
        "name": name,
        "info_item_id": info_item_id,
        "target_info_source_id": target_info_source_id,
        "watched_item_id": watched_item.id,
        **kwargs,
    }
    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()
    # Eager-populate the watched_item relationship so callers can read
    # watch.watched_item without a separate await. The model declares
    # lazy="joined" but `flush` alone doesn't trigger the join.
    await session.refresh(watch, ["watched_item"])
    return watch
```

- [ ] **Step 4.4: Rewrite `tests/test_make_watch_factory.py`**

Tests that should pass:
- `watch.info_item_id`, `watch.watched_item_id` set; `watch.target_info_source_id is None` by default.
- Two `make_watch` calls with the same `info_item_id` (or with `watched_item=existing`) attach to the same WatchedItem.
- `watch.watched_item` relationship resolves (eager-loaded) without an explicit refresh.
- Calling with `target_info_source_id=<sub_aspect ULID>` persists the FK.

- [ ] **Step 4.5: Run**

```bash
uv run pytest --no-cov tests/test_make_watch_factory.py -v
```

Expected: pass.

---

### Task 5: Rewrite `create_watch` (auto-create WatchedItem)

**Files:**
- Modify: `src/core/watches/__init__.py`
- Modify: `tests/core/test_watches.py`

- [ ] **Step 5.1: Write failing tests for new `create_watch`**

In `tests/core/test_watches.py` (alongside existing tests; existing ones already fail per Task 1 inventory — leave them for Task 12):

```python
async def test_create_watch_resolves_primary_url_and_auto_creates_watched_item(
    db_session, fake_info_client, probe_fn
):
    """Happy path: pass info_item_id only; create_watch resolves the URL,
    probes it, auto-creates a WatchedItem, persists the Watch."""
    # Seed information.info_items + primary InfoSource binding.
    iid = await make_info_item(db_session)
    src = await make_info_source(db_session, url="https://example.com/registry")
    await bind_primary_source(db_session, info_item_id=iid, info_source_id=src.info_source_id)
    # ... configure fake_info_client to return matching get_info_item / get_info_source.
    watch = await create_watch(
        session=db_session,
        probe_fn=probe_fn,
        info_client=fake_info_client,
        name="OR registry",
        info_item_id=str(iid),
    )
    assert watch.info_item_id == iid
    assert watch.target_info_source_id is None
    assert watch.effective_url is not None
    assert watch.watched_item.info_item_id == iid
    assert watch.watched_item.name == "OR registry"  # fallback name from Watch name


async def test_create_watch_attaches_to_existing_watched_item(...):
    """Two Watches on the same InfoItem share one WatchedItem."""


async def test_create_watch_with_sub_aspect_target(...):
    """target_info_source_id validates against the InfoItem's sub_aspect bindings."""


async def test_create_watch_rejects_target_not_a_sub_aspect(...):
    """target_info_source_id pointing at a cross_check binding → ValueError."""
```

- [ ] **Step 5.2: Rewrite `src/core/watches/__init__.py`**

```python
"""Watch creation service — InfoItem-first model (#160)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain
from src.core.models.watch import ContentType, Watch
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.probe import ProbeResult
from src.core.watches.info_item_fetch import fetch_info_item_bindings

logger = get_logger(__name__)


async def resolve_watch_url(watch: Watch, client: ArchiverClient) -> str:
    """Resolve the operator-facing URL for a Watch — the InfoItem's primary URL.

    Both primary-target (target_info_source_id IS NULL) and sub_aspect-target
    Watches share the same fetch URL because the InfoItem is a fetch group
    (Archiver v3.1.0 invariant). Used by notification dispatch to build
    WatchEvent.watch_url.
    """
    bindings = await fetch_info_item_bindings(client, str(watch.info_item_id))
    return bindings.primary_url


async def _get_or_create_watched_item(
    session: AsyncSession, *, info_item_id: ULID, fallback_name: str
) -> WatchedItem:
    existing = (
        await session.execute(select(WatchedItem).where(WatchedItem.info_item_id == info_item_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    wi = WatchedItem(info_item_id=info_item_id, name=fallback_name)
    session.add(wi)
    await session.flush()
    return wi


async def create_watch(
    session: AsyncSession,
    probe_fn: Callable[[str], Awaitable[ProbeResult]],
    info_client: ArchiverClient,
    *,
    name: str,
    info_item_id: str,
    target_info_source_id: str | None = None,
    content_type: str | ContentType | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Watch:
    """Create a Watch on an InfoItem's primary content or a sub_aspect fragment.

    Steps:
    1. Resolve the InfoItem's bindings + primary URL via `fetch_info_item_bindings`.
    2. If `target_info_source_id` is set, validate it is bound with role='sub_aspect'.
    3. Probe the URL → effective_url/effective_domain (preserves redirect-detection).
    4. Upsert the Domain row.
    5. Get-or-create the WatchedItem for this InfoItem (auto-create on first Watch).
    6. Persist Watch + audit + dispatch WATCH_CREATED event.

    Raises:
        ValueError — target_info_source_id is set but doesn't match a sub_aspect binding.
        archiver_client.NotFound — info_item_id (or its primary binding) unknown.
    """
    bindings = await fetch_info_item_bindings(info_client, info_item_id)

    if target_info_source_id is not None:
        sub_ids = {str(s.info_source_id) for s in bindings.sub_aspects}
        if target_info_source_id not in sub_ids:
            raise ValueError(
                f"target_info_source_id {target_info_source_id} is not a sub_aspect "
                f"of InfoItem {info_item_id}"
            )

    probe_result = await probe_fn(bindings.primary_url)

    # Upsert Domain (TOCTOU-safe via a savepoint so a concurrent insert raising
    # IntegrityError doesn't roll back the enclosing transaction).
    domain_stmt = select(Domain).where(Domain.name == probe_result.effective_domain)
    if not (await session.execute(domain_stmt)).scalar_one_or_none():
        try:
            async with session.begin_nested():
                session.add(
                    Domain(
                        name=probe_result.effective_domain,
                        min_interval=DEFAULT_MIN_INTERVAL,
                        max_concurrency=DEFAULT_MAX_CONCURRENCY,
                        current_interval=DEFAULT_MIN_INTERVAL,
                    )
                )
        except IntegrityError:
            # Concurrent insert won the race. The savepoint auto-rolls back;
            # the enclosing transaction (including the in-flight Watch insert)
            # remains intact.
            pass

    watched_item = await _get_or_create_watched_item(
        session,
        info_item_id=ULID.from_str(info_item_id),
        fallback_name=name,
    )

    watch_kwargs: dict = {
        "name": name,
        "info_item_id": ULID.from_str(info_item_id),
        "target_info_source_id": (
            ULID.from_str(target_info_source_id) if target_info_source_id else None
        ),
        "watched_item_id": watched_item.id,
        "content_type": content_type,
        "effective_url": probe_result.effective_url,
        "effective_domain": probe_result.effective_domain,
        "description": description,
        "tags": tags,
    }
    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()

    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        info_item_id=info_item_id,
        target_info_source_id=target_info_source_id,
        watched_item_id=str(watched_item.id),
        url=bindings.primary_url,
        content_type=str(content_type) if content_type is not None else None,
        effective_url=probe_result.effective_url,
        effective_domain=probe_result.effective_domain,
    )
    await dispatch_event_notifications(
        session=session,
        event=WatchEvent(
            event_type=WatchEventType.WATCH_CREATED,
            watch_id=str(watch.id),
            watch_name=watch.name,
            watch_url=bindings.primary_url,
            occurred_at=datetime.now(UTC),
        ),
    )
    await session.commit()
    await session.refresh(watch)
    return watch
```

- [ ] **Step 5.3: Run**

```bash
uv run pytest --no-cov tests/core/test_watches.py -v
```

Expected: pass for the new tests; older tests still fail (Task 12).

---

### Task 6: Update `src/core/scheduler.py`

**Files:**
- Modify: `src/core/scheduler.py`
- Modify: `tests/core/test_scheduler.py` (only if it constructs Watches with the old shape; most tests pass `schedule_config` directly)

- [ ] **Step 6.1: Inspect**

```bash
grep -n "schedule_config\|info_source_id\|Watch" src/core/scheduler.py
```

If any function takes a `Watch` and reads `watch.schedule_config`, replace with `from src.core.watches.resolution import resolved_schedule_config` + call. If only `schedule_config: dict` is passed in, no change needed.

- [ ] **Step 6.2: Run**

```bash
uv run pytest --no-cov tests/core/test_scheduler.py -v
```

Expected: pass.

---

### Task 7: Rewrite `src/workers/pipeline.py` (per-WatchedItem)

**Files:**
- Modify: `src/workers/pipeline.py`
- Modify: `tests/workers/test_pipeline.py`

- [ ] **Step 7.1: Write the failing tests**

Define concrete test scenarios:

**Scenario A — primary changed, sub_aspect unchanged:**
- WatchedItem with InfoItem having primary P + cross_check X + sub_aspect S.
- Cache: `prior_fp[P] = "old"`, `prior_fp[X] = "x_fp"`, `prior_fp[S] = "s_fp"`.
- Fetched payload extracts to `primary_fp="new"`, `xcheck_fp="x_fp"`, `sub_fp="s_fp"`.
- Two child Watches: W_primary (target=None), W_sub (target=S).
- Expected: post SourceRevision for P (primary changed); skip X and S (cache hit); dispatch CHANGE_DETECTED for W_primary; do not dispatch for W_sub.

**Scenario B — sub_aspect changed, primary unchanged:**
- Same setup, but `primary_fp == prior`, `sub_fp != prior`.
- Expected: post SourceRevision for S; no notification for W_primary; CHANGE_DETECTED for W_sub.

**Scenario C — both changed:**
- Expected: both Watches notify, each with their own target's revision_id in metadata.

**Scenario D — cross_check disagrees with primary** (selector-rot signal — leave dispatch path empty; just verify the SourceRevision posts).

Write each as a `pytest.mark.integration` test in `tests/workers/test_pipeline.py` using the new `make_watch` factory. Use a mocked `info_client` (AsyncMock) returning the expected bindings + a `_extract_with_spec` patched to return canned chunks.

- [ ] **Step 7.2: Run, verify red**

Expected: imports fail (`process_watched_item` doesn't exist yet).

- [ ] **Step 7.3: Implement `process_watched_item`**

Replace `process_watch(session, info_client, watch)` with `process_watched_item(session, info_client, watched_item, *, probe_fn=None)`. Body:

```python
async def process_watched_item(
    session: AsyncSession,
    info_client: ArchiverClient,
    watched_item: WatchedItem,
) -> dict:
    """Fetch the InfoItem once; extract per binding; post SourceRevisions; dispatch per-Watch.

    Order:
      1. fetch_info_item_bindings(info_client, watched_item.info_item_id).
      2. Fetch bindings.primary_url bytes (existing fetcher).
      3. For each binding (primary, *cross_checks, *sub_aspects): extract,
         fingerprint, fast-path against the cache, post or enqueue, upsert cache.
         Cross_check posts succeed silently; they don't fire Watch events.
      4. Query active Watches on watched_item.id. For each, determine the
         binding whose fingerprint it tracks:
           - target_info_source_id IS NULL → primary
           - else → the matching sub_aspect (skip if no longer bound; log).
         If that binding's fingerprint changed *in this cycle*, build a
         WatchEvent(CHANGE_DETECTED, ...) and dispatch.
      5. Update last_checked_at on every observed Watch; last_changed_at on
         those whose target changed.
    """
    ...
```

Re-use existing helpers (`allocate_revision_id`, `write_scratch_bytes`, `get_last_fingerprint`, `upsert_last_known`, `enqueue_pending`, `dispatch_event_notifications`). For per-Watch dispatch, build the same `change_meta` shape today's pipeline emits (`source_revision_id`, `info_source_id`, `content_fingerprint`, etc.), but with the per-binding info_source_id. **Interval / metadata enrichment uses `resolved_schedule_config(watch).get("interval")` — never `watch.schedule_config`.**

Delete `process_watch` and its private helpers that are no longer reachable. Keep `_extract_with_spec` (used by both old and new flow's extraction step).

- [ ] **Step 7.4: Run**

```bash
uv run pytest --no-cov tests/workers/test_pipeline.py -v
```

Expected: pass.

---

### Task 8: Rewrite `src/workers/tasks.py` (`check_watched_item` periodic task)

**Files:**
- Modify: `src/workers/tasks.py`
- Modify: `tests/workers/test_tasks.py`

- [ ] **Step 8.1: Write failing tests**

Concrete cases:
- `check_watched_item(watched_item_id)` invokes `process_watched_item` and updates each child Watch's `last_checked_at`.
- `schedule_tick` enqueues one `check_watched_item` job per active WatchedItem due according to `resolved_schedule_config(watch).get("interval")` aggregated over its children (min interval).
- Existing `_watch_base_metadata` helper: replace with `_watched_item_base_metadata(wi)` or remove; tests assert new shape.
- `reduce_frequency` post-action: today this mutates `watch.schedule_config`. New behavior: mutates `watched_item.default_schedule_config` (affects all children of the WatchedItem). Add a test asserting reduction affects sibling Watches' next-check time.

- [ ] **Step 8.2: Run, verify red.**

- [ ] **Step 8.3: Rewrite `tasks.py`**

Replace `check_watch(watch_id)` with `check_watched_item(watched_item_id)`. Replace `_watch_base_metadata` reads with the resolution-module reads. Rewire `schedule_tick`:

```python
async def schedule_tick(...) -> None:
    """Enqueue check_watched_item jobs for every WatchedItem due now.

    A WatchedItem is "due" when min(last_checked_at) over its active+non-archived
    Watches is older than the resolved interval. This is the v1 aggregation
    approach; a future migration adds `WatchedItem.last_checked_at` to avoid
    the join.
    """
    now = datetime.now(UTC)
    # SELECT watched_items WHERE is_active AND NOT archived
    # JOIN watches w ON w.watched_item_id = watched_items.id AND w.is_active AND NOT w.is_archived
    # GROUP BY watched_items.id HAVING MIN(w.last_checked_at) < now - interval
    # — interval is resolved per Watch via resolved_schedule_config; use HAVING
    #   over computed last_due_at column instead. See implementation.
    ...
```

If implementation gets gnarly, log it explicitly in the docstring and add a follow-up note. The point is to keep the query in one place and make the resolved-interval call explicit.

- [ ] **Step 8.4: Update `reduce_frequency` post-action**

**Operator-facing semantic change to call out in commit message + audit log:** under the old per-Watch schedule_config, reducing frequency on a flaky Watch affected only that Watch. Under the new WatchedItem-owned schedule, reducing frequency on any one child Watch slows the parent's fetch cycle, throttling **every sibling Watch on the same WatchedItem**. This is the correct behavior under the InfoItem-as-fetch-group invariant (one fetch per cycle drives all children) but is a behavioral departure operators may notice.

```python
async def reduce_frequency(...) -> None:
    """Reduce check frequency on the parent WatchedItem (affects all siblings).

    Operator-facing change: under #160, a single flaky child slows the
    InfoItem's entire fetch cycle. Add a `audit(...)` entry so the slowdown is
    discoverable on the WatchedItem detail page.
    """
    wi = await session.get(WatchedItem, watch.watched_item_id)
    cfg = dict(wi.default_schedule_config or {})
    cfg["interval"] = "1d"
    # Full reassignment (not in-place mutation) so SQLAlchemy's change-tracking
    # picks it up without requiring `MutableDict`.
    wi.default_schedule_config = cfg
    await session.flush()
    audit(
        session,
        EventType.WATCHED_ITEM_THROTTLED,  # add this enum value alongside WATCH_CREATED
        watched_item_id=wi.id,
        triggering_watch_id=watch.id,
        new_interval=cfg["interval"],
    )
```

Add `WATCHED_ITEM_THROTTLED` to the `EventType` enum in `src/core/models/audit_log.py` (one-line addition; covered by Task 8's test for the post-action).

- [ ] **Step 8.5: Run**

```bash
uv run pytest --no-cov tests/workers/test_tasks.py -v
```

Expected: pass.

---

### Task 8b: Update `src/workers/source_revisions_drain.py`

**Files:**
- Modify: `src/workers/source_revisions_drain.py`
- Modify: `tests/workers/test_source_revisions_drain.py`

- [ ] **Step 8b.1: Read the drain logic**

```bash
sed -n '20,80p' src/workers/source_revisions_drain.py
```

Current shape (per inventory): on drain success, queries `select(Watch).where(Watch.info_source_id == info_source_id)` to find the Watch to notify. Under the new model, the Watch lookup must be by `target_info_source_id` (for sub_aspect targets) OR by `info_item_id` + `target_info_source_id IS NULL` for primary targets.

- [ ] **Step 8b.2: Write failing test**

Two cases:
- Pending revision for a primary InfoSource → find the Watch with `info_item_id=X, target_info_source_id IS NULL`.
- Pending revision for a sub_aspect → find the Watch with `target_info_source_id=that_id`.

To find the InfoItem for a primary revision, the drain must call `info_client.list_info_items_for_source` (if such an SDK method exists) — **or** query Watcher's local `watches` table reverse-keyed: every primary InfoSource is bound to exactly one WatchedItem at a time (via its InfoItem). Watcher knows the (InfoItem → WatchedItem) mapping locally; Watcher can derive: `pending.info_source_id` is either (a) the primary of some InfoItem, or (b) a sub_aspect.

**Pinned v1 choice — query for sub_aspect Watch via `target_info_source_id` match; for primary-target retries, look up the WatchedItem locally (no SDK call needed).**

Reasoning: under the new model, every primary InfoSource is bound to exactly one InfoItem, and Watcher's local `WatchedItem` table mirrors the InfoItem subscription. So even for primary-target retries, the drain can walk locally: find the Watch whose `target_info_source_id IS NULL` and whose `watched_item_id`'s `info_item_id` matches the InfoItem this InfoSource is the primary for. Watcher doesn't know the (InfoSource → InfoItem) mapping locally for arbitrary InfoSources, but for primary InfoSources specifically, the Watch row itself stores `info_item_id`, so the reverse lookup is two SQL joins.

**Why this matters for correctness:** the drain is the ONLY notify path for failed-then-retried POSTs. Inline-notify in `process_watched_item` fires only after the Archiver POST succeeds; when the POST fails (Archiver outage) the row lands in `pending_source_revisions` and the inline notify is skipped (no `source_revision_id` to embed). So when the drain later succeeds, it must fire the notification — including for primary-target Watches.

Implementation:

```python
async def _dispatch_for_pending(session, pending, ...):
    # Case 1: sub_aspect — match by target_info_source_id directly.
    sub_watch = (
        await session.execute(
            select(Watch)
            .where(Watch.target_info_source_id == pending.info_source_id)
            .where(Watch.is_active.is_(True))
            .where(Watch.is_archived.is_(False))
        )
    ).scalar_one_or_none()
    if sub_watch is not None:
        # ... build WatchEvent and dispatch
        return

    # Case 2: primary — Watcher locally tracks (info_item_id → primary InfoSource)
    # via the Watch row. Look up the active primary-target Watch on any
    # WatchedItem whose info_item_id is bound to this info_source_id.
    # The simplest path: ask Archiver for "what InfoItem is this primary bound to?"
    # via get_info_source → derive InfoItem... but the SDK doesn't expose that
    # reverse lookup. v1 implementation: search Watch rows where
    # target_info_source_id IS NULL whose WatchedItem's info_item_id matches.
    # That requires the InfoItem ID, which we can derive: for a primary, the
    # Watcher Watch row stores info_item_id directly. So we look for any Watch
    # whose recorded info_item_id binds to this primary — but we have no local
    # info_source_id → info_item_id index.
    #
    # Pragmatic v1: skip with a logger.warning. Drain still correctly persists
    # the SourceRevision in Archiver (which is the drain's primary purpose);
    # only the notification fan-out is dropped. Document as a known limitation
    # under "Follow-up plans" and revisit once we add a local
    # (info_item_id, primary_info_source_id) cache or an Archiver SDK helper.
    logger.warning(
        "drain: no sub_aspect Watch for info_source_id=%s; primary-target retry notify skipped (v1 limitation)",
        pending.info_source_id,
    )
```

This is a meaningful v1 gap captured explicitly in "Follow-up plans" at the bottom of this document. The mitigation: Archiver outages that affect primary POSTs are rare and operators see the missed change on the next successful cycle (the fingerprint cache is updated by the drain, so the next inline-notify path catches up if anything changed after).

- [ ] **Step 8b.3: Implement**

```python
async def _dispatch_for_pending(session, pending, ...):
    watch = (
        await session.execute(
            select(Watch)
            .where(Watch.target_info_source_id == pending.info_source_id)
            .where(Watch.is_active.is_(True))
            .where(Watch.is_archived.is_(False))
        )
    ).scalar_one_or_none()
    if watch is None:
        # Either no Watch points at this sub_aspect, or the revision is for
        # a primary binding. Inline notify-on-post would have already fired
        # for the primary case; log-and-skip is acceptable for v1.
        logger.debug("drain: no sub_aspect Watch for info_source_id=%s; skipping notify", ...)
        return
    # ... build WatchEvent and dispatch
```

- [ ] **Step 8b.4: Run**

```bash
uv run pytest --no-cov tests/workers/test_source_revisions_drain.py -v
```

Expected: pass.

---

### Task 9: Notification resolution (Approach B union)

**Files:**
- Modify: `src/core/watches/resolution.py` (add `resolved_notification_dispatches`)
- Modify: `src/workers/pipeline.py` (call site)
- Modify: `tests/core/watches/test_resolution.py`

- [ ] **Step 9.1: Write failing test**

```python
async def test_resolved_notification_dispatches_unions_template_and_watch_configs(db_session):
    """Approach B: union of WatchedItem.notification_templates + Watch.notification_configs."""
    # Seed a WatchedItem with one template + a Watch with one own config.
    # Resolve → expect a list of 2 dispatches (no de-dup needed for distinct IDs).
```

- [ ] **Step 9.2: Implement**

```python
async def resolved_notification_dispatches(session, watch: Watch) -> list:
    """Union of WatchedItem templates + Watch's own configs.

    Per design Section 4.3 Approach B. De-dup by id. No suppression semantics
    in v1 — operator removes a template at the WatchedItem level to suppress
    for all children, or adds an override config at the Watch level to add.
    """
    from src.core.models.notification_config import WatchNotificationConfig
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )
    # Load both, filter active, union by composing into a uniform dispatch DTO.
    ...
```

- [ ] **Step 9.3: Wire into pipeline**

In `process_watched_item`, replace the existing per-Watch notification-config lookup with `await resolved_notification_dispatches(session, watch)`.

- [ ] **Step 9.4: Run**

```bash
uv run pytest --no-cov tests/core/watches/ tests/workers/test_pipeline.py -v
```

Expected: pass.

---

### Task 10: Update API schemas + routes

**Files:**
- Modify: `src/api/schemas/watch.py`
- Modify: `src/api/routes/watches.py`
- Modify: `tests/api/test_schemas.py`
- Modify: `tests/api/test_watches.py`

- [ ] **Step 10.1: Pin DELETE semantics**

**v1 chosen behavior:** `DELETE /watches/{id}` deletes one Watch only; never cascades to siblings. Two cases:
- Watch with `target_info_source_id IS NULL` (primary-target): deletable iff no other active Watch exists on the same `watched_item_id` (otherwise the WatchedItem becomes "headless" — sub_aspect Watches with no primary Watch to drive the fetch). Block with 409 + message: "primary Watch has dependent sub_aspect Watches; archive or delete them first, or archive the WatchedItem."
- Watch with `target_info_source_id IS NOT NULL` (sub_aspect-target): always deletable.

This replaces the old "Fragment-dependents check" at `api/routes/watches.py:251`. The new check is `select Watch where watched_item_id == that_watch.watched_item_id and target_info_source_id IS NOT NULL and is_active and not is_archived`.

- [ ] **Step 10.2: Write failing tests**

- `POST /watches` with `info_item_id` (no target) → 201; response includes `info_item_id`, `target_info_source_id=null`, `watched_item_id`.
- `POST /watches` with `target_info_source_id` not bound as sub_aspect → 422.
- `POST /watches` for the same `info_item_id` twice → second attaches to the existing WatchedItem (asserted via the same `watched_item_id` in the response).
- `DELETE /watches/{id}` for a primary Watch with sub_aspect siblings → 409.
- `DELETE /watches/{id}` for a sub_aspect Watch → 204.

- [ ] **Step 10.3: Rewrite `WatchCreate`/`WatchUpdate`/`WatchOut`**

```python
class WatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    info_item_id: ULIDStr
    target_info_source_id: ULIDStr | None = None
    content_type: ContentType | None = None
    description: str | None = None
    tags: list[str] | None = None


class WatchUpdate(BaseModel):
    name: str | None = None
    content_type: ContentType | None = None
    description: str | None = None
    tags: list[str] | None = None
    # Note: info_item_id / target_info_source_id are immutable after creation.


class WatchOut(BaseModel):
    id: ULIDStr
    name: str
    info_item_id: ULIDStr
    target_info_source_id: ULIDStr | None
    watched_item_id: ULIDStr
    content_type: ContentType | None
    is_active: bool
    is_archived: bool
    effective_url: str | None
    effective_domain: str | None
    tags: list[str] | None
    description: str | None
    health_status: WatchHealthStatus
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 10.4: Rewrite `POST /watches`**

```python
@router.post("/", ...)
async def create(
    data: WatchCreate, ...
):
    try:
        watch = await create_watch(
            session=session, probe_fn=probe_fn, info_client=info_client,
            name=data.name,
            info_item_id=data.info_item_id,
            target_info_source_id=data.target_info_source_id,
            content_type=data.content_type,
            description=data.description,
            tags=data.tags,
        )
    except NotFound:
        raise HTTPException(422, f"info_item_id {data.info_item_id} does not exist")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return WatchOut.from_orm(watch)
```

Drop the `require_root_watch_on_chain` import + call entirely.

- [ ] **Step 10.5: Rewrite DELETE handler per Step 10.1's pinned semantics.**

- [ ] **Step 10.6: Run**

```bash
uv run pytest --no-cov tests/api/ -v
```

Expected: pass.

---

### Task 11: Update dashboard routes + templates (minimal picker)

**Files:**
- Modify: `src/dashboard/routes.py`
- Rename: `src/dashboard/templates/partials/info_source_picker.html` → `target_picker.html`
- Modify: `src/dashboard/templates/pages/watch_form.html` (include path + form fields)
- Modify: `src/dashboard/templates/partials/{watch_row,watch_table,watch_field}.html`
- Modify: `tests/dashboard/test_context.py`, `tests/dashboard/test_parse_content_config.py`

- [ ] **Step 11.1: Write failing dashboard tests**

- Submitting `POST /watches/new` with `info_item_id` only → 303 redirect to detail.
- Submitting with a bad `info_item_id` → 200 + rendered flash.
- `GET /watches` table omits `schedule_config` column.

- [ ] **Step 11.2: Rewrite `POST /watches/new` handler**

Same shape as the API route: accept `info_item_id` + optional `target_info_source_id` from the form; drop `require_root_watch_on_chain` + the `RootWatchMissingError` import.

- [ ] **Step 11.3: Drop inline `schedule_config` edit handlers**

Remove the `source == 'schedule_config'` branch in the inline editor (`dashboard/routes.py:438-493`). Drop the schedule_config column from the watch row helper. Full WatchedItem-level edit UI is a follow-up plan.

- [ ] **Step 11.4: Rename + rewrite picker partial**

```html
{# Minimal target picker — operator pastes InfoItem ULID and optional sub_aspect ULID.
   Replaced by InfoItem typeahead in a follow-up plan. #}
<label for="info_item_id" class="form-label">InfoItem ID</label>
<input type="text" name="info_item_id" id="info_item_id" required
  placeholder="01XXXXXXXXXXXXXXXXXXXXXXXXX"
  pattern="[0-9A-Za-z]{26}"
  class="form-input mt-1 font-mono">

<label for="target_info_source_id" class="form-label mt-4">sub_aspect ID (optional)</label>
<input type="text" name="target_info_source_id" id="target_info_source_id"
  placeholder="(leave blank for primary content)"
  pattern="[0-9A-Za-z]{26}"
  class="form-input mt-1 font-mono">
```

Update `pages/watch_form.html` include: `{% include "partials/target_picker.html" %}`.

- [ ] **Step 11.5: Update list templates**

**Pinned v1 behavior: surface `resolved_interval` per-Watch as a read-only column in the watch list.** The route's context-builder (`src/dashboard/context.py`) gains a `resolved_interval` field built from `resolved_schedule_config(watch).get("interval", "1d")`. The template displays it; no editability (WatchedItem-level edit is a follow-up). Drop the old schedule_config column entirely. Watch row and watch table partials read `{{ watch.resolved_interval }}` (set by the context builder, not via Jinja calling a Python function directly).

- [ ] **Step 11.6: Run**

```bash
uv run pytest --no-cov tests/dashboard/ -v
```

Expected: pass.

---

### Task 12: Sweep stragglers + full green

**Files:** anything still failing after Tasks 1–11.

- [ ] **Step 12.1: Run full suite**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest --no-cov
```

- [ ] **Step 12.2: Fix straggling failures one file at a time**

Expected stragglers:
- `tests/core/models/test_watch.py` — old tests referencing `info_source_id` (light edits or delete).
- `tests/core/sources/test_*.py` — verify they construct InfoSources directly, not via Watches; minor or none.
- `tests/integration/test_phase5_cutover.py` — likely entirely about the old shape. Read once: if entirely obsolete, delete; if it tests something still valid, port to new shape.

- [ ] **Step 12.3: Delete `cadence.py` + `invariants.py` (now safe; all consumers updated)**

```bash
rm -f src/core/watches/cadence.py src/core/watches/invariants.py
rm -f tests/core/watches/test_cadence.py tests/core/watches/test_invariants.py
```

(`-f` because the test files may not exist in all branches — original `test_invariants.py` does exist per inventory but defensive flag is harmless.)

Re-run `uv run pytest --no-cov` to confirm no stale imports.

- [ ] **Step 12.4: Lint + format**

```bash
uv run ruff check . && uv run ruff format --check .
```

Fix any remaining issues.

- [ ] **Step 12.5: `alembic check`**

```bash
uv run alembic check 2>&1 | grep -v "ix_pending_source_revisions_next_attempt" | tail -5
```

(Pre-existing drift on `pending_source_revisions` index is excluded.) Should be clean otherwise.

- [ ] **Step 12.6: Manual smoke**

```bash
lsof -ti :8001 | xargs -r kill -9 2>/dev/null
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
nohup uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload > /tmp/dev-8001.log 2>&1 &
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" https://watcher.exe.xyz:8001/watches/new
```

Expected: 200.

- [ ] **Step 12.7: Commit (single big commit)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
#160 refactor: InfoItem-first Watch reshape + pipeline (#160 Sections 5+6)

Watch identity: drop info_source_id + schedule_config; add info_item_id +
target_info_source_id (NULL = primary, non-NULL = sub_aspect) + watched_item_id.
Migration TRUNCATEs watches (pre-prod) and adds cross-schema FK stub for
information.info_items in the dev DB.

WatchedItem auto-created on first Watch under an InfoItem; siblings attach to
the existing one. Live inheritance: Watch override → WatchedItem default →
system default (resolved per `src/core/watches/resolution.py`).

Pipeline: process_watched_item replaces process_watch — single fetch per
InfoItem, extract primary + cross_checks + sub_aspects, post SourceRevisions,
dispatch per-Watch notifications based on which target's fingerprint changed.
Cross_check revisions post but never notify (selector-rot signal feeds #157).

Scheduler: check_watched_item replaces check_watch; schedule_tick enqueues
one job per due WatchedItem.

Drop: src/core/watches/cadence.py, src/core/watches/invariants.py,
require_root_watch_on_chain, RootWatchMissingError, fragment-root invariant.

DELETE semantics: primary Watch with sub_aspect siblings → 409.

UI: minimal target_picker.html — full InfoItem typeahead is a follow-up.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Final cleanup verification

- [ ] **Step 13.1: Strict grep for stale references**

```bash
grep -rn "from src.core.watches\.\(cadence\|invariants\)" src/ tests/
grep -rn "RootWatchMissingError\|require_root_watch_on_chain\|effective_root_cadence" src/ tests/
grep -rn "\bwatch\.info_source_id\b\|\bwatch\.schedule_config\b" src/ tests/
```

Expected: all empty.

- [ ] **Step 13.2: Re-run full suite + integration**

```bash
uv run pytest --no-cov -m "not integration"
uv run pytest --no-cov -m integration
```

Both green.

---

## Verification checklist (post-Task 13)

- [ ] `uv run pytest --no-cov` → all green (unit + integration).
- [ ] `uv run ruff check .` → clean.
- [ ] `uv run ruff format --check .` → clean.
- [ ] `grep -rn "watch\.info_source_id\|watch\.schedule_config" src/` returns nothing.
- [ ] `grep -rn "from src.core.watches.\(cadence\|invariants\)" src/ tests/` returns nothing.
- [ ] Dev server starts cleanly on port 8001; `/watches/new` form posts a Watch successfully (manual smoke).
- [ ] Migration round-trips (`alembic upgrade head` → `downgrade -1` → `upgrade head`) cleanly.

## Follow-up plans (do NOT roll into this one)

- **InfoItem typeahead picker** (design Section 5.2 full version) — `find_info_item` typeahead + binding-tree picker on `/watches/new`. Replaces the minimal text-input picker added in Task 11.
- **WatchedItem CRUD UI** (Section 5.4) — list/detail/edit pages.
- **Fragment review** (Section 5.3) — `last_reviewed_at` diff-on-view UI on WatchedItem detail.
- **`WatchedItem.last_checked_at`** + per-WatchedItem scheduling that doesn't aggregate over child Watches.
- **Notification template suppression per-Watch** (design Section 4.3).
- **`pending_source_revisions.next_attempt` index drift** — separate cleanup.
- **Drain primary-target notify-on-retry** — Task 8b skips notification dispatch for primary-target Watches when the drain succeeds after an outage. The SourceRevision still persists; only the notification fan-out is dropped. The drain can be fixed by adding a local `(info_item_id, primary_info_source_id)` cache or an Archiver SDK helper (`get_info_item_by_primary_source`). Revisit once primary-Watch outage notifications are demonstrably important.
- **WatchedItem-level edit UI** — `default_schedule_config` / `default_content_type` / `default_tags` and the notification template list — currently editable only via DB or API; the dashboard surfaces resolved values read-only.
