# Phase 5 Watcher v2 Cutover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Watcher to produce `SourceRevisions` in Archiver (instead of locally-stored `Snapshot`s), reshape `Watch` to bind `info_source_id`, and dispatch notifications from POST-success sites instead of a Redis change bus.

**Architecture:** Clean cutover with no compat shim. Watcher pre-allocates ULIDs for each `SourceRevision`, writes extracted bytes to a scratch file under that ULID, then POSTs to Archiver (v2.2.0+ accepts the client-supplied id). Cascade-from-cached-bytes extracts root + N fragment revisions per fetch. A `pending_source_revisions` outbox buffers POSTs when Archiver is unreachable; sweeper-interlock keeps scratch files alive across deferred POSTs. Notification dispatch reuses today's `dispatch_event_notifications` — pipeline emits `WatchEvent.CHANGE_DETECTED` with `source_revision_id` in metadata; `notify.py`'s diff-loading path goes away with `Change`/`Snapshot`.

**Tech Stack:** Python 3.12+, FastAPI, async SQLAlchemy, Alembic, Procrastinate (periodic tasks), `archiver-client>=2.2.0,<3`, `notifier-client` (existing).

**Anchored design:** [`docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md`](./2026-05-13-phase-5-watcher-v2-cutover.md) — read this first. Tracks against [GH #156](https://github.com/CannObserv/watcher/issues/156).

---

## Pre-flight

### Branch + worktree

Pick one:

- **Worktree (recommended for parallel work):**
  ```bash
  cd /home/exedev/watcher
  git worktree add .worktrees/feat-156-phase5-cutover -b feat/156-phase5-cutover
  cd .worktrees/feat-156-phase5-cutover
  uv sync
  ```
  Dev server lives on port 8001; production stays on 8000 via systemd.

- **Direct on main (matches user's feedback memory):** one stage = one commit. Smaller blast radius per commit; risk of half-cutover states on main between stages.

### Environment

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
echo "DATABASE_URL=$DATABASE_URL"  # must be set
echo "ARCHIVER_BASE_URL=$ARCHIVER_BASE_URL"
echo "ARCHIVER_API_KEY=${ARCHIVER_API_KEY:0:6}…"
```

### Archiver SDK readiness check

```bash
grep '__version__' /home/exedev/archiver/clients/python/src/archiver_client/__init__.py
# Expected: __version__ = "2.2.0"
```

Confirm methods that *do* exist (used in this plan):
```bash
grep -E "    async def (post_source_revision|patch_source_revision_cache|get_info_source|list_info_sources|get_info_item)" \
  /home/exedev/archiver/clients/python/src/archiver_client/client.py
# Expected: 5 lines.
```

Methods that **do not** exist on the v2.2.0 SDK (deliberately routed around by this plan):
- `list_source_revisions` — Watcher uses a local `last_known_revisions` table instead (Task 0.1).
- `get_primary_info_source_for_item` — migration script takes a CLI-supplied manifest (Task 0.2 + Task 5.4).

### Pre-cutover operator wiring

**This is a prerequisite, not a code task.** Before Task 5.4 (migration), the operator must wire each watched `info_item_id` to its primary `info_source_id` in Archiver via the SDK (`add_info_source(info_item_id, info_source_id, role="primary")`).

Current production state at plan-start (2026-05-13):
- 3 Watches in Watcher DB
- 9 InfoItems in Archiver DB
- 0 active `info_item_sources` rows

After wiring, each watched item must have exactly one row with `role='primary'` and `deactivated_at IS NULL`. The schema enforces uniqueness; verify with:
```bash
DATABASE_URL='postgresql+asyncpg://archiver:archiver@localhost:5432/archiver' \
  uv run python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(text(\"SELECT info_item_id, info_source_id FROM information.info_item_sources WHERE deactivated_at IS NULL AND role='primary'\"))
        for row in r:
            print(row)
asyncio.run(main())
"
```

### TDD discipline

Per AGENTS.md: "TDD required. Red → Green → Refactor. No production code without a failing test first." Every task includes a "Write the failing test" step. Skipping it is a process violation.

---

## File Structure

### Files created

| Path | Responsibility |
|---|---|
| `src/core/models/last_known_revision.py` | ORM + table holding Watcher's local cache of (info_source_id → latest fingerprint) |
| `src/core/models/pending_source_revision.py` | ORM for the `pending_source_revisions` outbox |
| `src/core/sources/__init__.py` | Package init |
| `src/core/sources/resolver.py` | `resolve_root_sources_with_children` SDK wrapper + dataclasses |
| `src/core/sources/scratch.py` | Scratch-file write + ULID allocation + rename safety net |
| `src/core/sources/outbox.py` | `pending_source_revisions` select/enqueue/delete helpers |
| `src/core/sources/revision_cache.py` | `last_known_revisions` upsert + lookup |
| `src/workers/source_revisions_drain.py` | Procrastinate periodic task draining the outbox |
| `src/workers/cache_sweeper.py` | Procrastinate periodic task sweeping scratch files |
| `src/core/watches/__init__.py` | Package init (if not present) |
| `src/core/watches/invariants.py` | Fragment-root invariants for Watch create + delete |
| `src/core/watches/cadence.py` | `min(root.schedule, min(fragment_schedules))` |
| `scripts/migrate_watches_to_v2.py` | One-shot Watch.info_item_id → Watch.info_source_id migration |
| Alembic migrations:<br>`<rev>_create_last_known_revisions.py`<br>`<rev>_create_pending_source_revisions.py`<br>`<rev>_add_watches_info_source_id.py`<br>`<rev>_drop_watches_info_item_id_etc.py`<br>`<rev>_drop_snapshots_changes.py` | Schema changes |
| Tests mirroring each of the above |

### Files modified

| Path | Change |
|---|---|
| `pyproject.toml` | SDK pin `>=2.2.0,<3` |
| `src/core/models/watch.py` | Add `info_source_id`, drop `info_item_id`; add cross-schema FK stub for `information.info_sources` |
| `src/workers/tasks.py` | `check_watch` uses new resolver; URL/domain threading; emits WatchEvent with `source_revision_id` |
| `src/workers/pipeline.py` | Fetch → scratch → POST root → cascade per fragment |
| `src/api/routes/watches.py` | Create/delete invariants, cascade override |
| `src/api/main.py` | Remove `start_changes_drain_loop()` lifespan call |
| `src/core/notifications/notify.py` | Drop `_load_event_unified_diff` (Change/Snapshot gone); fragment-aware template vars |
| `src/core/notifications/events.py` | Document `metadata["source_revision_id"]` convention |
| `src/dashboard/routes.py` | Delete Snapshot/Change-related routes (lines ~395-515 + ~2289) |
| `src/dashboard/context.py` | Drop Change summary builder; drop Change detail context |
| `src/dashboard/templates/...` | Drop Snapshot/Change-rendering templates |
| `tests/conftest.py` | Remove `trg_changes_update_last_changed_at` trigger recreation; drop `make_change`/`make_snapshot` fixtures |
| `CHANGELOG.md` | Phase 5 cutover entry |
| `AGENTS.md` | Remove the `tools/info_changes_consumer.py` reference; update DB Triggers section |

### Files deleted

| Path | Reason |
|---|---|
| `src/core/info_resolver.py` | Replaced by `src/core/sources/resolver.py` |
| `src/core/changes/publisher.py` | Watcher no longer produces `info.changes` |
| `src/core/changes/outbox.py` | Replaced by `src/core/sources/outbox.py` |
| `src/core/changes/redis_url.py` | Helper for the removed Redis publisher |
| `src/core/changes/__init__.py` (and directory) | Empty after deletions |
| `src/workers/changes_drain.py` | Periodic + fast-tick drain both gone |
| `src/core/models/snapshot.py` | Local content persistence dropped |
| `src/core/models/change.py` | Local change-record table dropped |
| `src/core/differ.py` | Chunk-level diff dropped |
| `src/api/routes/changes.py` | Entire route module consumed Change + Snapshot |
| `tools/info_changes_consumer.py` | No more Watcher-side consumer |
| All test files mirroring the above (incl. `tests/workers/test_phase2c_drain_integration.py`, `tests/workers/test_changes_drain_loop.py`, `tests/core/changes/`) |

### Files possibly retained (verify in Task 10.7)

| Path | Reason |
|---|---|
| `src/core/simhash.py` | Mirrored to Archiver per AGENTS.md "Mirrored content-acquisition code." If Archiver still imports it, keep with a "mirror parity, not used by Watcher" header comment. |

---

## Stage 0 — Pre-flight tasks

Two SDK gaps (`list_source_revisions`, `get_primary_info_source_for_item`) are routed around by Watcher-local solutions. Land these *before* any code that depends on them.

### Task 0.1: Local `last_known_revisions` cache table

Watcher's pipeline fast-path needs to compare a freshly-computed fingerprint against the previous successful POST. Archiver doesn't expose `list_source_revisions`; rather than add an SDK method, Watcher keeps a small local table.

**Files:**
- Create: `src/core/models/last_known_revision.py`
- Create: `alembic/versions/<rev>_create_last_known_revisions.py`
- Create: `src/core/sources/__init__.py` (empty)
- Create: `src/core/sources/revision_cache.py`
- Test: `tests/core/models/test_last_known_revision.py`
- Test: `tests/core/sources/test_revision_cache.py`

**Steps:**

- [ ] **Step 1: Write the failing model round-trip test**

  ```python
  # tests/core/models/test_last_known_revision.py
  """Round-trip tests for last_known_revisions."""
  from datetime import UTC, datetime

  import pytest
  from sqlalchemy import select

  from src.core.models.last_known_revision import LastKnownRevision

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_last_known_revision_round_trip(db_session):
      row = LastKnownRevision(
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint="sha256:" + "a" * 64,
          source_revision_id="01HZZ00000000000000000REV",
          captured_at=datetime.now(UTC),
      )
      db_session.add(row)
      await db_session.flush()

      fetched = (await db_session.execute(
          select(LastKnownRevision).where(
              LastKnownRevision.info_source_id == row.info_source_id
          )
      )).scalar_one()
      assert fetched.content_fingerprint == row.content_fingerprint
  ```

- [ ] **Step 2: Run, confirm failure**

  ```bash
  uv run pytest tests/core/models/test_last_known_revision.py --no-cov 2>&1 | tail -5
  ```
  Expected: ImportError.

- [ ] **Step 3: Write the model**

  ```python
  # src/core/models/last_known_revision.py
  """Watcher-local cache of the most recent SourceRevision per InfoSource.

  Used by the pipeline fast-path to skip POST when the freshly-computed
  fingerprint matches the previous one. Eliminates the need for an
  `list_source_revisions` SDK method.

  Keyed by info_source_id (primary key, not the row's ULID) — there's
  exactly one row per source.
  """
  from datetime import datetime

  from sqlalchemy import DateTime, Text
  from sqlalchemy.orm import Mapped, mapped_column
  from ulid import ULID

  from src.core.models.base import Base, ULIDType


  class LastKnownRevision(Base):
      """Watcher-local fingerprint cache, one row per info_source_id."""

      __tablename__ = "last_known_revisions"

      info_source_id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True)
      content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
      source_revision_id: Mapped[ULID] = mapped_column(ULIDType, nullable=False)
      captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
  ```

- [ ] **Step 4: Re-export from package init**

  Add to `src/core/models/__init__.py`:
  ```python
  from src.core.models.last_known_revision import LastKnownRevision
  ```
  Update `__all__` accordingly.

- [ ] **Step 5: Generate the migration**

  ```bash
  uv run alembic revision --autogenerate -m "create last_known_revisions cache"
  ```

  Hand-verify: `op.create_table("last_known_revisions", ...)` with `info_source_id` as PK. Reverse `downgrade()` drops the table.

- [ ] **Step 6: Apply + run model test**

  ```bash
  uv run alembic upgrade head
  uv run pytest tests/core/models/test_last_known_revision.py --no-cov 2>&1 | tail -5
  ```
  Expected: green.

- [ ] **Step 7: Write the failing cache-helper tests**

  ```python
  # tests/core/sources/test_revision_cache.py
  """Helpers around last_known_revisions."""
  from datetime import UTC, datetime

  import pytest

  from src.core.sources.revision_cache import (
      get_last_fingerprint,
      upsert_last_known,
  )

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_get_returns_none_when_no_prior(db_session):
      assert await get_last_fingerprint(db_session, "01HZZ...UNSEEN") is None


  @pytest.mark.asyncio
  async def test_upsert_then_get_returns_fingerprint(db_session):
      await upsert_last_known(
          db_session,
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint="sha256:" + "a" * 64,
          source_revision_id="01HZZ00000000000000000REV",
          captured_at=datetime.now(UTC),
      )
      fp = await get_last_fingerprint(db_session, "01HZZ00000000000000000000F")
      assert fp == "sha256:" + "a" * 64


  @pytest.mark.asyncio
  async def test_upsert_overwrites_prior(db_session):
      kw = dict(
          info_source_id="01HZZ00000000000000000000F",
          captured_at=datetime.now(UTC),
      )
      await upsert_last_known(
          db_session,
          content_fingerprint="sha256:" + "a" * 64,
          source_revision_id="01HZZ00000000000000000REV",
          **kw,
      )
      await upsert_last_known(
          db_session,
          content_fingerprint="sha256:" + "b" * 64,
          source_revision_id="01HZZ00000000000000NEWREV",
          **kw,
      )
      fp = await get_last_fingerprint(db_session, "01HZZ00000000000000000000F")
      assert fp == "sha256:" + "b" * 64
  ```

- [ ] **Step 8: Run, confirm failure**

  ```bash
  uv run pytest tests/core/sources/test_revision_cache.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 9: Write the helpers**

  ```python
  # src/core/sources/revision_cache.py
  """Read/write the Watcher-local last_known_revisions table."""
  from datetime import datetime

  from sqlalchemy import select
  from sqlalchemy.dialects.postgresql import insert as pg_insert
  from sqlalchemy.ext.asyncio import AsyncSession

  from src.core.models.last_known_revision import LastKnownRevision


  async def get_last_fingerprint(
      session: AsyncSession,
      info_source_id: str,
  ) -> str | None:
      """Return the cached fingerprint for `info_source_id`, or None."""
      result = await session.execute(
          select(LastKnownRevision.content_fingerprint).where(
              LastKnownRevision.info_source_id == info_source_id
          )
      )
      return result.scalar_one_or_none()


  async def upsert_last_known(
      session: AsyncSession,
      *,
      info_source_id: str,
      content_fingerprint: str,
      source_revision_id: str,
      captured_at: datetime,
  ) -> None:
      """Upsert the cache row for `info_source_id` (PK-keyed)."""
      stmt = (
          pg_insert(LastKnownRevision)
          .values(
              info_source_id=info_source_id,
              content_fingerprint=content_fingerprint,
              source_revision_id=source_revision_id,
              captured_at=captured_at,
          )
          .on_conflict_do_update(
              index_elements=["info_source_id"],
              set_={
                  "content_fingerprint": content_fingerprint,
                  "source_revision_id": source_revision_id,
                  "captured_at": captured_at,
              },
          )
      )
      await session.execute(stmt)
      await session.flush()
  ```

- [ ] **Step 10: Run, confirm pass**

  ```bash
  uv run pytest tests/core/sources/test_revision_cache.py --no-cov 2>&1 | tail -5
  ```
  Expected: 3 passed.

- [ ] **Step 11: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: Watcher-local last_known_revisions cache for pipeline fast-path"
  ```

### Task 0.2: Lock the migration strategy (no new SDK helper needed)

`scripts/migrate_watches_to_v2.py` accepts a `--manifest` JSON map (`info_item_id → info_source_id`) supplied by the operator. No `get_primary_info_source_for_item` SDK call.

**Files:**
- *No code yet* — this is a decision record. Task 5.4 implements.

**Steps:**

- [ ] **Step 1: Document the decision**

  Add to this plan's "Risks + open questions" — already captured (verify next reviewer pass).

- [ ] **Step 2: No commit; proceed to Stage 1.**

---

## Stage 1 — SDK pin + error envelope migration

### Task 1.1: Bump archiver-client SDK pin

**Files:**
- Modify: `pyproject.toml`

**Steps:**

- [ ] **Step 1: Inspect current pin**

  ```bash
  grep -n "archiver-client" pyproject.toml
  ```
  Expected: two lines — the dep list and the path-dep table. Note line numbers; they vary.

- [ ] **Step 2: Edit the path-dep table to add version constraint**

  Find:
  ```toml
  archiver-client = { path = "/home/exedev/archiver/clients/python", editable = true }
  ```
  Replace with:
  ```toml
  archiver-client = { path = "/home/exedev/archiver/clients/python", editable = true, version = ">=2.2.0,<3" }
  ```

- [ ] **Step 3: Re-sync**

  ```bash
  uv sync 2>&1 | tail -5
  ```
  Expected: `archiver-client==2.2.0` resolved.

- [ ] **Step 4: Quick smoke**

  ```bash
  uv run python -c "from archiver_client import ArchiverClient; import inspect; sig = inspect.signature(ArchiverClient.post_source_revision); print('source_revision_id' in sig.parameters)"
  ```
  Expected: `True`.

- [ ] **Step 5: Commit**

  ```bash
  git add pyproject.toml uv.lock
  git commit -m "#156 chore: pin archiver-client>=2.2.0,<3 for Phase 5 cutover"
  ```

### Task 1.2: Sweep existing error handling to unified envelope

**Files:**
- Modify (sweep): `src/core/`, `src/workers/`, `src/api/`, `src/dashboard/`

**Steps:**

- [ ] **Step 1: Locate existing archiver error catches**

  ```bash
  grep -rn "InformationError\|except.*archiver_client\|archiver_client.*errors" src/ 2>&1 | grep -v ".pyc"
  ```

- [ ] **Step 2: For each site, update to `Conflict` / `InformationError`**

  Pattern (new):
  ```python
  from archiver_client import Conflict, InformationError
  try:
      await client.post_source_revision(...)
  except Conflict as e:
      logger.warning("archiver conflict", extra={"kind": e.kind, "data": e.data})
  except InformationError as e:
      logger.error("archiver error", extra={"kind": e.kind, "errors": e.errors})
  ```

- [ ] **Step 3: Run impacted tests**

  ```bash
  uv run pytest -k "archiver or info_client or resolver" --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 4: Commit (skip if no-op)**

  ```bash
  git add -A
  git commit -m "#156 refactor: migrate archiver error catches to Conflict/InformationError envelope"
  ```

---

## Stage 2 — Remove `info.changes` producer plumbing

### Task 2.1: Delete `src/core/changes/` and its consumer tool

The directory holds publisher, outbox, and Redis-URL helpers. None survive the cutover.

**Files:**
- Delete: `src/core/changes/publisher.py`
- Delete: `src/core/changes/outbox.py`
- Delete: `src/core/changes/redis_url.py`
- Delete: `src/core/changes/__init__.py` + the empty directory
- Delete: `tools/info_changes_consumer.py`
- Delete: `tests/core/changes/` (entire directory)

**Steps:**

- [ ] **Step 1: Inventory and confirm no surviving importers outside the workers drain (which Task 2.2 removes)**

  ```bash
  grep -rn "from src.core.changes" src/ tests/ tools/ 2>&1 | grep -v ".pyc"
  ```
  Expected importers: `src/workers/changes_drain.py` (deleted in Task 2.2), maybe a few tests. Note them all.

- [ ] **Step 2: List current tests for baseline**

  ```bash
  uv run pytest tests/core/changes/ --no-cov 2>&1 | tail -3
  ```
  Note the count for later comparison.

- [ ] **Step 3: Delete the directory + consumer tool**

  ```bash
  git rm -r src/core/changes/ tests/core/changes/
  git rm tools/info_changes_consumer.py
  ```

- [ ] **Step 4: Update docs**

  ```bash
  grep -rn "info_changes_consumer\|tools/info_changes_consumer" docs/ AGENTS.md CLAUDE.md
  ```
  AGENTS.md has it under "Operational scripts (`tools/`)" — remove that line.

- [ ] **Step 5: Re-collect to find broken imports**

  ```bash
  uv run pytest --collect-only 2>&1 | tail -20
  ```
  Expected: failures only in `src/workers/changes_drain.py` and `tests/workers/test_changes_drain.py` / `tests/workers/test_changes_drain_loop.py` / `tests/workers/test_phase2c_drain_integration.py`. Task 2.2 cleans these.

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop src/core/changes/ + tools/info_changes_consumer.py"
  ```

### Task 2.2: Delete drain workers + lifespan plumbing

**Files:**
- Delete: `src/workers/changes_drain.py`
- Delete: `tests/workers/test_changes_drain.py`
- Delete: `tests/workers/test_changes_drain_loop.py` (if present)
- Delete: `tests/workers/test_phase2c_drain_integration.py` (if present)
- Modify: `src/api/main.py` (drop the `start_changes_drain_loop()` call from lifespan)
- Modify: `src/workers/__init__.py` if it imports from `changes_drain`

**Steps:**

- [ ] **Step 1: Inspect the lifespan call site**

  ```bash
  grep -n "changes_drain\|start_changes_drain_loop\|stop_changes_drain_loop\|drain_changes_outbox" src/api/main.py src/workers/__init__.py
  ```
  Capture exact line numbers — the constant `DRAIN_ADVISORY_LOCK_ID` and the loop functions live in `src/workers/changes_drain.py`; `src/api/main.py` only invokes them.

- [ ] **Step 2: Write a failing test that asserts the lifespan no longer starts the loop**

  ```python
  # tests/api/test_main_lifespan.py — create or append
  """Lifespan should not register changes-drain after Phase 5 cutover."""
  import pytest
  from src.api.main import app

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_lifespan_does_not_start_changes_drain():
      async with app.router.lifespan_context(app):
          # No app.state attribute referencing the drain.
          for attr in dir(app.state):
              assert "changes_drain" not in attr.lower()
              assert "drain_changes" not in attr.lower()
  ```

- [ ] **Step 3: Run, confirm failure**

  ```bash
  uv run pytest tests/api/test_main_lifespan.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 4: Strip the lifespan call**

  In `src/api/main.py`, find and remove the block that calls `start_changes_drain_loop(...)` and any matching cleanup in the `finally` clause. The exact identifier varies — `grep` for `changes_drain` from Step 1 to find it.

- [ ] **Step 5: Delete the periodic-task module and its tests**

  ```bash
  git rm src/workers/changes_drain.py
  git rm tests/workers/test_changes_drain.py
  git rm tests/workers/test_changes_drain_loop.py 2>/dev/null
  git rm tests/workers/test_phase2c_drain_integration.py 2>/dev/null
  ```

- [ ] **Step 6: Update `src/workers/__init__.py`**

  Remove `from src.workers.changes_drain import ...` if any.

- [ ] **Step 7: Verify nothing else references the deleted symbols**

  ```bash
  grep -rn "DRAIN_ADVISORY_LOCK_ID\|CHANGES_DRAIN_INTERVAL_SECONDS\|start_changes_drain_loop\|stop_changes_drain_loop\|drain_changes_outbox" src/ tests/
  ```
  Expected: empty output (or only the test from Step 2).

- [ ] **Step 8: Run targeted suites**

  ```bash
  uv run pytest tests/api/test_main_lifespan.py tests/workers/ --no-cov 2>&1 | tail -5
  ```
  Expected: green.

- [ ] **Step 9: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: remove changes_drain worker + lifespan plumbing"
  ```

### Task 2.3: Drop `Change.published_to_bus_at` and `Change.bus_message_id` columns

The `Change` table is dropped wholesale in Task 10.2; this task just removes the bus-related columns so the model + tests stay coherent until then.

Actually — since Task 10.2 drops the entire `Change` table including these columns, this task is **a no-op** if Task 10.2 is run in the same plan execution. Defer to Task 10.2 and skip this task. Mark as done by deletion.

**Action:** No-op. Task 10.2 covers it.

---

## Stage 3 — Outbox infrastructure (`pending_source_revisions`)

### Task 3.1: Model + migration

**Files:**
- Create: `src/core/models/pending_source_revision.py`
- Create: `alembic/versions/<rev>_create_pending_source_revisions.py`
- Test: `tests/core/models/test_pending_source_revision.py`

**Steps:**

- [ ] **Step 1: Write the failing model round-trip test**

  ```python
  # tests/core/models/test_pending_source_revision.py
  """Round-trip tests for pending_source_revisions."""
  from datetime import UTC, datetime, timedelta

  import pytest
  from sqlalchemy import select
  from sqlalchemy.exc import IntegrityError

  from src.core.models.pending_source_revision import PendingSourceRevision

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_round_trip(db_session):
      now = datetime.now(UTC)
      row = PendingSourceRevision(
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint="sha256:" + "a" * 64,
          captured_at=now,
          content_cache_uri="file:///var/cache/watcher/scratch/01JV0000000000000000000000.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
          next_attempt_at=now,
      )
      db_session.add(row)
      await db_session.flush()

      fetched = (await db_session.execute(
          select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
      )).scalar_one()
      assert fetched.attempts == 0
      assert fetched.last_error is None
      assert fetched.content_fingerprint == row.content_fingerprint


  @pytest.mark.asyncio
  async def test_unique_source_and_fingerprint(db_session):
      now = datetime.now(UTC)
      kw = dict(
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint="sha256:" + "a" * 64,
          captured_at=now,
          content_cache_uri="file:///x.bin",
          content_cache_expires_at=now,
          next_attempt_at=now,
      )
      db_session.add(PendingSourceRevision(**kw))
      await db_session.flush()
      db_session.add(PendingSourceRevision(**kw))
      with pytest.raises(IntegrityError):
          await db_session.flush()
  ```

- [ ] **Step 2: Run, confirm failure**

  ```bash
  uv run pytest tests/core/models/test_pending_source_revision.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 3: Write the model**

  ```python
  # src/core/models/pending_source_revision.py
  """Watcher-side outbox for SourceRevisions awaiting POST to Archiver.

  Inserted when Archiver is unreachable (network, 5xx, 401). Drain worker
  retries with backoff and clears rows on success.

  The `id` column doubles as the client-supplied `source_revision_id` —
  Watcher allocates the ULID up-front and references the scratch file
  `<id>.bin` from `content_cache_uri`.
  """
  from datetime import datetime

  from sqlalchemy import BigInteger, DateTime, Integer, Text, UniqueConstraint, func
  from sqlalchemy.orm import Mapped, mapped_column
  from ulid import ULID

  from src.core.models.base import Base, ULIDType, generate_ulid


  class PendingSourceRevision(Base):
      """A SourceRevision waiting to be POSTed to Archiver."""

      __tablename__ = "pending_source_revisions"

      id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
      info_source_id: Mapped[ULID] = mapped_column(ULIDType, nullable=False, index=True)
      content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
      captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
      content_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
      content_media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
      content_cache_uri: Mapped[str] = mapped_column(Text, nullable=False)
      content_cache_expires_at: Mapped[datetime] = mapped_column(
          DateTime(timezone=True), nullable=False
      )
      attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
      last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
      next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
      created_at: Mapped[datetime] = mapped_column(
          DateTime(timezone=True), nullable=False, server_default=func.now()
      )

      __table_args__ = (
          UniqueConstraint(
              "info_source_id", "content_fingerprint",
              name="uq_pending_source_revisions_source_fingerprint",
          ),
      )
  ```

- [ ] **Step 4: Re-export from package init**

  Append to `src/core/models/__init__.py`:
  ```python
  from src.core.models.pending_source_revision import PendingSourceRevision
  ```

- [ ] **Step 5: Generate the migration**

  ```bash
  uv run alembic revision --autogenerate -m "create pending_source_revisions outbox"
  ```

- [ ] **Step 6: Hand-verify the migration**

  Open the generated file. Confirm:
  - `op.create_table("pending_source_revisions", …)` with all columns.
  - Index on `info_source_id` (autogenerated from `index=True`).
  - Partial index for the drain query — **add manually** if missing:
    ```python
    op.create_index(
        "ix_pending_source_revisions_next_attempt",
        "pending_source_revisions",
        ["next_attempt_at"],
        postgresql_where=sa.text("attempts < 10"),
    )
    ```
  - Reverse `downgrade()` drops both indexes + table.

- [ ] **Step 7: Apply + run the test**

  ```bash
  uv run alembic upgrade head
  uv run pytest tests/core/models/test_pending_source_revision.py --no-cov 2>&1 | tail -5
  ```
  Expected: green.

- [ ] **Step 8: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: pending_source_revisions outbox model + migration"
  ```

### Task 3.2: Outbox helpers

**Files:**
- Create: `src/core/sources/outbox.py`
- Test: `tests/core/sources/test_outbox.py`

**Steps:**

- [ ] **Step 1: Write failing helper tests**

  ```python
  # tests/core/sources/test_outbox.py
  """Helpers for pending_source_revisions."""
  from datetime import UTC, datetime, timedelta

  import pytest

  from src.core.models.pending_source_revision import PendingSourceRevision
  from src.core.sources.outbox import (
      delete_pending,
      enqueue_pending,
      mark_failure,
      select_due,
  )

  pytestmark = pytest.mark.integration

  FP = "sha256:" + "a" * 64


  @pytest.mark.asyncio
  async def test_enqueue_pending_writes_row(db_session):
      row = await enqueue_pending(
          db_session,
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint=FP,
          captured_at=datetime.now(UTC),
          content_cache_uri="file:///x.bin",
          content_cache_expires_at=datetime.now(UTC) + timedelta(seconds=600),
      )
      assert row.id is not None
      assert row.attempts == 0
      assert row.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=1)


  @pytest.mark.asyncio
  async def test_select_due_excludes_future(db_session):
      now = datetime.now(UTC)
      future = await enqueue_pending(
          db_session,
          info_source_id="01HZZ00000000000000000000A",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///a.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
      )
      future.next_attempt_at = now + timedelta(hours=1)
      due = await enqueue_pending(
          db_session,
          info_source_id="01HZZ00000000000000000000B",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///b.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
      )
      await db_session.flush()
      rows = await select_due(db_session, limit=10)
      ids = {r.id for r in rows}
      assert due.id in ids
      assert future.id not in ids


  @pytest.mark.asyncio
  async def test_mark_failure_advances_backoff(db_session):
      now = datetime.now(UTC)
      row = await enqueue_pending(
          db_session,
          info_source_id="01HZZ00000000000000000000C",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///c.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
      )
      await mark_failure(db_session, row, error="ConnectionError")
      assert row.attempts == 1
      assert row.last_error == "ConnectionError"
      assert row.next_attempt_at > now


  @pytest.mark.asyncio
  async def test_delete_pending_removes_row(db_session):
      from sqlalchemy import select
      now = datetime.now(UTC)
      row = await enqueue_pending(
          db_session,
          info_source_id="01HZZ00000000000000000000D",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///d.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
      )
      await delete_pending(db_session, row.id)
      result = await db_session.execute(
          select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
      )
      assert result.scalar_one_or_none() is None
  ```

- [ ] **Step 2: Run, confirm failure**

  ```bash
  uv run pytest tests/core/sources/test_outbox.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 3: Write the helpers**

  ```python
  # src/core/sources/outbox.py
  """Helpers for the pending_source_revisions outbox."""
  from datetime import UTC, datetime, timedelta

  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import AsyncSession
  from ulid import ULID

  from src.core.models.pending_source_revision import PendingSourceRevision

  _BACKOFF_CAP_SECONDS = 3600


  def _backoff_seconds(attempts: int) -> int:
      """Exponential backoff with a 1-hour cap. attempts=1 → 60s."""
      return min(60 * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)


  async def enqueue_pending(
      session: AsyncSession,
      *,
      info_source_id: str,
      content_fingerprint: str,
      captured_at: datetime,
      content_cache_uri: str,
      content_cache_expires_at: datetime,
      content_size_bytes: int | None = None,
      content_media_type: str | None = None,
  ) -> PendingSourceRevision:
      """Insert a new outbox row. next_attempt_at = now."""
      row = PendingSourceRevision(
          info_source_id=info_source_id,
          content_fingerprint=content_fingerprint,
          captured_at=captured_at,
          content_size_bytes=content_size_bytes,
          content_media_type=content_media_type,
          content_cache_uri=content_cache_uri,
          content_cache_expires_at=content_cache_expires_at,
          next_attempt_at=datetime.now(UTC),
      )
      session.add(row)
      await session.flush()
      return row


  async def select_due(
      session: AsyncSession, *, limit: int = 100
  ) -> list[PendingSourceRevision]:
      """Return rows due for retry, oldest-first, with FOR UPDATE SKIP LOCKED."""
      result = await session.execute(
          select(PendingSourceRevision)
          .where(PendingSourceRevision.next_attempt_at <= datetime.now(UTC))
          .where(PendingSourceRevision.attempts < 10)
          .order_by(PendingSourceRevision.next_attempt_at.asc())
          .limit(limit)
          .with_for_update(skip_locked=True)
      )
      return list(result.scalars().all())


  async def mark_failure(
      session: AsyncSession,
      row: PendingSourceRevision,
      *,
      error: str,
  ) -> None:
      """Increment attempts, record error, advance next_attempt_at."""
      row.attempts += 1
      row.last_error = error
      row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=_backoff_seconds(row.attempts))


  async def delete_pending(session: AsyncSession, row_id: ULID) -> None:
      """Remove a successfully-drained row."""
      row = await session.get(PendingSourceRevision, row_id)
      if row is not None:
          await session.delete(row)
  ```

- [ ] **Step 4: Run, confirm pass**

  ```bash
  uv run pytest tests/core/sources/test_outbox.py --no-cov 2>&1 | tail -5
  ```
  Expected: 4 passed.

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: outbox helpers for pending_source_revisions"
  ```

### Task 3.3: Drain worker (Procrastinate periodic task, stubbed dispatch)

The drain worker writes WatchEvent and calls `dispatch_event_notifications` like the rest of the codebase — no new dispatcher. The WatchEvent's `metadata["source_revision_id"]` is the new payload field (Stage 8 finalizes how `notify.py` consumes it; this task just emits the field).

**Files:**
- Create: `src/workers/source_revisions_drain.py`
- Test: `tests/workers/test_source_revisions_drain.py`

**Steps:**

- [ ] **Step 1: Write the failing test**

  ```python
  # tests/workers/test_source_revisions_drain.py
  """Drain worker for pending_source_revisions."""
  from datetime import UTC, datetime, timedelta
  from unittest.mock import AsyncMock, MagicMock, patch

  import httpx
  import pytest
  from sqlalchemy import select

  from src.core.models.pending_source_revision import PendingSourceRevision
  from src.workers.source_revisions_drain import drain_pending_source_revisions

  pytestmark = pytest.mark.integration

  FP = "sha256:" + "a" * 64


  @pytest.mark.asyncio
  async def test_drain_posts_and_deletes_on_success(db_session, monkeypatch):
      """Successful POST → row deleted; WatchEvent dispatched."""
      now = datetime.now(UTC)
      row = PendingSourceRevision(
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///x.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
          next_attempt_at=now,
      )
      db_session.add(row)
      await db_session.commit()

      fake_client = MagicMock()
      fake_client.post_source_revision = AsyncMock(return_value=MagicMock(
          source_revision_id=str(row.id),
          content_fingerprint=FP,
      ))
      fake_dispatch = AsyncMock()

      # Watcher's pattern: monkeypatch get_registry/get_session_factory.
      from src.workers import source_revisions_drain as mod
      monkeypatch.setattr(
          mod, "get_session_factory",
          lambda: _async_session_factory_returning(db_session),
      )
      monkeypatch.setattr(
          mod, "_get_archiver_client", lambda: fake_client,
      )
      monkeypatch.setattr(
          mod, "dispatch_event_notifications", fake_dispatch,
      )

      result = await drain_pending_source_revisions(batch_size=10)
      assert result["drained"] == 1
      assert result["failed"] == 0
      fake_client.post_source_revision.assert_awaited_once()
      fake_dispatch.assert_awaited_once()
      # WatchEvent metadata carries source_revision_id.
      call_args = fake_dispatch.await_args
      event = call_args.args[1] if len(call_args.args) >= 2 else call_args.kwargs["event"]
      assert event.metadata.get("source_revision_id") == str(row.id)


  @pytest.mark.asyncio
  async def test_drain_marks_failure_on_archiver_error(db_session, monkeypatch):
      """ConnectError → row.attempts++, last_error set, row remains."""
      now = datetime.now(UTC)
      row = PendingSourceRevision(
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint=FP,
          captured_at=now,
          content_cache_uri="file:///x.bin",
          content_cache_expires_at=now + timedelta(seconds=600),
          next_attempt_at=now,
      )
      db_session.add(row)
      await db_session.commit()

      fake_client = MagicMock()
      fake_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("nope"))
      from src.workers import source_revisions_drain as mod
      monkeypatch.setattr(mod, "_get_archiver_client", lambda: fake_client)
      monkeypatch.setattr(mod, "dispatch_event_notifications", AsyncMock())
      monkeypatch.setattr(
          mod, "get_session_factory",
          lambda: _async_session_factory_returning(db_session),
      )

      result = await drain_pending_source_revisions(batch_size=10)
      assert result["drained"] == 0
      assert result["failed"] == 1

      stored = (await db_session.execute(
          select(PendingSourceRevision).where(PendingSourceRevision.id == row.id)
      )).scalar_one()
      assert stored.attempts == 1
      assert stored.last_error and "ConnectError" in stored.last_error
      assert stored.next_attempt_at > now
  ```

  > **Note on `_async_session_factory_returning`:** existing watcher tests use a similar pattern. Search `grep -rn "session_factory" tests/workers/ tests/conftest.py` and reuse the existing fixture / helper.

- [ ] **Step 2: Run, confirm failure**

  ```bash
  uv run pytest tests/workers/test_source_revisions_drain.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 3: Write the drain worker**

  ```python
  # src/workers/source_revisions_drain.py
  """Periodic Procrastinate task draining pending_source_revisions to Archiver."""
  from datetime import UTC, datetime

  from sqlalchemy import select

  from src.core.database import get_session_factory
  from src.core.logging import get_logger
  from src.core.models.pending_source_revision import PendingSourceRevision
  from src.core.models.watch import Watch
  from src.core.notifications.events import WatchEvent, WatchEventType
  from src.core.notifications.notify import dispatch_event_notifications
  from src.core.registry import get_registry
  from src.core.sources.outbox import delete_pending, mark_failure, select_due
  from src.core.sources.revision_cache import upsert_last_known
  from src.workers import bp

  logger = get_logger(__name__)


  def _get_archiver_client():
      return get_registry().get_archiver_client()


  async def _resolve_watch_for_source(session, info_source_id):
      """Return the active Watch (root or fragment) for this source, or None."""
      result = await session.execute(
          select(Watch)
          .where(Watch.info_source_id == info_source_id)
          .where(Watch.is_active.is_(True))
          .where(Watch.is_archived.is_(False))
      )
      return result.scalar_one_or_none()


  @bp.periodic(cron="* * * * *", periodic_id="drain_pending_source_revisions")
  @bp.task(name="drain_pending_source_revisions", queue="default")
  async def drain_pending_source_revisions(*, batch_size: int = 100, **periodic_kwargs) -> dict:
      """Drain due outbox rows: POST each, dispatch on success, mark_failure on error."""
      drained = 0
      failed = 0
      session_factory = get_session_factory()
      client = _get_archiver_client()

      async with session_factory() as session:
          rows = await select_due(session, limit=batch_size)
          for row in rows:
              try:
                  out = await client.post_source_revision(
                      info_source_id=str(row.info_source_id),
                      content_fingerprint=row.content_fingerprint,
                      captured_at=row.captured_at,
                      source_revision_id=str(row.id),
                      content_cache_uri=row.content_cache_uri,
                      content_cache_expires_at=row.content_cache_expires_at,
                      content_size_bytes=row.content_size_bytes,
                      content_media_type=row.content_media_type,
                  )
              except Exception as e:
                  await mark_failure(session, row, error=f"{type(e).__name__}: {e}")
                  failed += 1
                  logger.warning(
                      "drain attempt failed",
                      extra={"id": str(row.id), "attempts": row.attempts, "error": str(e)},
                  )
                  continue

              canonical_id = str(out.source_revision_id)

              await upsert_last_known(
                  session,
                  info_source_id=str(row.info_source_id),
                  content_fingerprint=row.content_fingerprint,
                  source_revision_id=canonical_id,
                  captured_at=row.captured_at,
              )

              watch = await _resolve_watch_for_source(session, str(row.info_source_id))
              if watch is not None:
                  event = WatchEvent(
                      event_type=WatchEventType.CHANGE_DETECTED,
                      watch_id=str(watch.id),
                      watch_name=watch.name,
                      watch_url=watch.effective_url or "",
                      occurred_at=datetime.now(UTC),
                      metadata={
                          "source_revision_id": canonical_id,
                          "info_source_id": str(row.info_source_id),
                          "content_fingerprint": row.content_fingerprint,
                          "deferred": True,
                      },
                  )
                  await dispatch_event_notifications(session, event)

              await delete_pending(session, row.id)
              drained += 1

          await session.commit()

      logger.info(
          "drain_pending_source_revisions finished",
          extra={"drained": drained, "failed": failed},
      )
      return {"drained": drained, "failed": failed}
  ```

- [ ] **Step 4: Run, confirm pass**

  ```bash
  uv run pytest tests/workers/test_source_revisions_drain.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: pending_source_revisions drain worker"
  ```

---

## Stage 4 — `resolve_root_sources_with_children` SDK wrapper

### Task 4.1: Resolver + dataclasses

**Files:**
- Create: `src/core/sources/resolver.py`
- Test: `tests/core/sources/test_resolver.py`

(`src/core/info_resolver.py` is deleted only after Stage 6 ports all callers — see Task 6.2 Step 7.)

**Steps:**

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/core/sources/test_resolver.py
  """resolve_root_sources_with_children walks parent chain + lists children."""
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from src.core.sources.resolver import (
      ResolvedFragmentSource,
      ResolvedRootSource,
      resolve_root_sources_with_children,
  )


  @pytest.mark.asyncio
  async def test_resolves_root_with_no_fragments():
      client = MagicMock()
      client.get_info_source = AsyncMock(return_value=MagicMock(
          info_source_id="01HZZ00000000000000000ROOT",
          parent_info_source_id=None,
          source_spec={
              "target": {"url": "https://example.com"},
              "extraction": {"algorithm": "full_page"},
          },
      ))
      client.list_info_sources = AsyncMock(return_value=MagicMock(items=[]))

      resolved = await resolve_root_sources_with_children(
          client, info_source_id="01HZZ00000000000000000ROOT"
      )
      assert isinstance(resolved, ResolvedRootSource)
      assert resolved.url == "https://example.com"
      assert resolved.children == []


  @pytest.mark.asyncio
  async def test_walks_parent_chain_to_root():
      client = MagicMock()
      client.get_info_source = AsyncMock(side_effect=[
          MagicMock(
              info_source_id="01HZZ00000000000000FRAGMENT",
              parent_info_source_id="01HZZ00000000000000000ROOT",
              source_spec={"extraction": {"algorithm": "css", "selector": "#main"}},
          ),
          MagicMock(
              info_source_id="01HZZ00000000000000000ROOT",
              parent_info_source_id=None,
              source_spec={"target": {"url": "https://example.com"}},
          ),
      ])
      client.list_info_sources = AsyncMock(return_value=MagicMock(items=[
          MagicMock(
              info_source_id="01HZZ00000000000000FRAGMENT",
              parent_info_source_id="01HZZ00000000000000000ROOT",
              source_spec={"extraction": {"algorithm": "css", "selector": "#main"}},
          ),
      ]))

      resolved = await resolve_root_sources_with_children(
          client, info_source_id="01HZZ00000000000000FRAGMENT"
      )
      assert resolved.info_source_id == "01HZZ00000000000000000ROOT"
      assert len(resolved.children) == 1
      assert resolved.children[0].info_source_id == "01HZZ00000000000000FRAGMENT"
  ```

- [ ] **Step 2: Run, confirm failure**

  ```bash
  uv run pytest tests/core/sources/test_resolver.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 3: Write the resolver**

  ```python
  # src/core/sources/resolver.py
  """Resolve a Watch's info_source_id to root + fragment SourceSpec docs."""
  from dataclasses import dataclass, field
  from typing import Any

  from archiver_client import ArchiverClient


  @dataclass(frozen=True)
  class ResolvedFragmentSource:
      info_source_id: str
      parent_info_source_id: str
      source_spec: dict[str, Any]


  @dataclass(frozen=True)
  class ResolvedRootSource:
      info_source_id: str
      url: str
      source_spec: dict[str, Any]
      children: list[ResolvedFragmentSource] = field(default_factory=list)


  def _spec_to_dict(spec: Any) -> dict[str, Any]:
      if hasattr(spec, "to_dict"):
          return dict(spec.to_dict())
      if hasattr(spec, "additional_properties"):
          return dict(spec.additional_properties)
      return dict(spec)


  async def resolve_root_sources_with_children(
      client: ArchiverClient,
      info_source_id: str,
  ) -> ResolvedRootSource:
      """Walk parent chain to root; list children of the root."""
      current = await client.get_info_source(info_source_id)
      while current.parent_info_source_id is not None:
          current = await client.get_info_source(str(current.parent_info_source_id))

      root = current
      root_spec = _spec_to_dict(root.source_spec)
      url = root_spec.get("target", {}).get("url")
      if not url:
          raise ValueError(f"root source {root.info_source_id} has no target.url")

      page = await client.list_info_sources(parent_info_source_id=str(root.info_source_id))
      children = [
          ResolvedFragmentSource(
              info_source_id=str(c.info_source_id),
              parent_info_source_id=str(c.parent_info_source_id),
              source_spec=_spec_to_dict(c.source_spec),
          )
          for c in page.items
      ]

      return ResolvedRootSource(
          info_source_id=str(root.info_source_id),
          url=url,
          source_spec=root_spec,
          children=children,
      )
  ```

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: resolve_root_sources_with_children + ResolvedRootSource/Fragment dataclasses"
  ```

---

## Stage 5 — Watch reshape

### Task 5.1: Add `watches.info_source_id` (nullable, transitional) + cross-schema FK stub

**Files:**
- Create: `alembic/versions/<rev>_add_watches_info_source_id.py`
- Modify: `src/core/models/watch.py`
- Test: `tests/core/models/test_watch.py`

**Steps:**

- [ ] **Step 1: Write failing test for the new column**

  Append to `tests/core/models/test_watch.py`:
  ```python
  @pytest.mark.asyncio
  async def test_watch_accepts_info_source_id(db_session):
      """Watch persists info_source_id alongside info_item_id (transitional)."""
      info_item = await make_info_item(db_session, name="T")
      watch = Watch(
          name="T",
          content_type=ContentType.HTML,
          info_item_id=info_item.info_item_id,
          info_source_id="01HZZ00000000000000000000F",
      )
      db_session.add(watch)
      await db_session.flush()
      fetched = (await db_session.execute(
          select(Watch).where(Watch.id == watch.id)
      )).scalar_one()
      assert str(fetched.info_source_id) == "01HZZ00000000000000000000F"
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Add the column + cross-schema FK stub to `src/core/models/watch.py`**

  Add the stub (mirrors the existing `info_items` stub):
  ```python
  # Cross-schema FK resolution stub for info_sources (mirrors info_items pattern).
  Table(
      "info_sources",
      Base.metadata,
      Column("info_source_id", ULIDType, primary_key=True),
      schema="information",
  )
  ```

  Add the column to `Watch`:
  ```python
  info_source_id: Mapped[ULID | None] = mapped_column(
      ULIDType,
      ForeignKey("information.info_sources.info_source_id", ondelete="RESTRICT"),
      nullable=True,
      index=True,
  )
  ```

- [ ] **Step 4: Generate migration**

  ```bash
  uv run alembic revision --autogenerate -m "add watches.info_source_id (nullable, transitional)"
  ```

  Hand-verify the FK targets `information.info_sources(info_source_id)`. Confirm `tests/conftest.py` already covers `information.info_sources` test-schema creation (it runs Archiver's alembic per AGENTS.md — verify with `grep -n "info_sources" tests/conftest.py`).

- [ ] **Step 5: Apply + test**

  ```bash
  uv run alembic upgrade head
  uv run pytest tests/core/models/test_watch.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: add watches.info_source_id column (nullable, transitional)"
  ```

### Task 5.2: Watch-create/delete invariants (fragment-root)

**Files:**
- Create: `src/core/watches/__init__.py` (empty if not present)
- Create: `src/core/watches/invariants.py`
- Test: `tests/core/watches/test_invariants.py`

**Steps:**

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/core/watches/test_invariants.py
  """Invariants enforced at the Watch lifecycle layer."""
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from src.core.watches.invariants import (
      FragmentDependentsExistError,
      RootWatchMissingError,
      require_no_fragment_dependents,
      require_root_watch_on_chain,
  )
  from tests.conftest import make_watch

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_require_root_watch_passes_when_root_is_watched(db_session):
      root_id = "01HZZ00000000000000000ROOT"
      frag_id = "01HZZ00000000000000FRAGMENT"
      await make_watch(db_session, info_source_id=root_id)
      client = MagicMock()
      client.get_info_source = AsyncMock(side_effect=[
          MagicMock(info_source_id=frag_id, parent_info_source_id=root_id),
          MagicMock(info_source_id=root_id, parent_info_source_id=None),
      ])
      await require_root_watch_on_chain(db_session, client, info_source_id=frag_id)


  @pytest.mark.asyncio
  async def test_require_root_watch_rejects_orphan(db_session):
      frag_id = "01HZZ00000000000000FRAGMENT"
      root_id = "01HZZ00000000000000000ROOT"
      client = MagicMock()
      client.get_info_source = AsyncMock(side_effect=[
          MagicMock(info_source_id=frag_id, parent_info_source_id=root_id),
          MagicMock(info_source_id=root_id, parent_info_source_id=None),
      ])
      with pytest.raises(RootWatchMissingError):
          await require_root_watch_on_chain(db_session, client, info_source_id=frag_id)


  @pytest.mark.asyncio
  async def test_require_no_dependents_blocks_when_fragments_exist(db_session):
      root_id = "01HZZ00000000000000000ROOT"
      frag_id = "01HZZ00000000000000FRAGMENT"
      root_watch = await make_watch(db_session, info_source_id=root_id)
      await make_watch(db_session, info_source_id=frag_id)
      client = MagicMock()
      client.list_info_sources = AsyncMock(return_value=MagicMock(items=[
          MagicMock(info_source_id=frag_id, parent_info_source_id=root_id),
      ]))
      with pytest.raises(FragmentDependentsExistError) as exc:
          await require_no_fragment_dependents(db_session, client, root_watch)
      assert frag_id in str(exc.value)
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Write the invariants**

  ```python
  # src/core/watches/invariants.py
  """Lifecycle invariants for root vs. fragment Watches."""
  from archiver_client import ArchiverClient
  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import AsyncSession

  from src.core.models.watch import Watch


  class RootWatchMissingError(Exception):
      """Fragment Watch creation without an active root Watch."""


  class FragmentDependentsExistError(Exception):
      """Root Watch delete attempted while fragment Watches depend on it."""


  async def _walk_to_root(client: ArchiverClient, info_source_id: str) -> list[str]:
      """Chain of info_source_ids from leaf → root."""
      chain = []
      current_id = info_source_id
      while current_id is not None:
          chain.append(current_id)
          source = await client.get_info_source(current_id)
          parent = source.parent_info_source_id
          current_id = str(parent) if parent is not None else None
      return chain


  async def require_root_watch_on_chain(
      session: AsyncSession,
      client: ArchiverClient,
      *,
      info_source_id: str,
  ) -> None:
      """No-op if a root Watch exists on the chain; raise otherwise."""
      chain = await _walk_to_root(client, info_source_id)
      result = await session.execute(
          select(Watch.id)
          .where(Watch.info_source_id.in_(chain))
          .where(Watch.is_active.is_(True))
          .where(Watch.is_archived.is_(False))
      )
      if result.scalar_one_or_none() is None:
          raise RootWatchMissingError(
              f"no active Watch on chain rooted at {chain[-1]} (target {info_source_id})"
          )


  async def require_no_fragment_dependents(
      session: AsyncSession,
      client: ArchiverClient,
      root_watch: Watch,
  ) -> None:
      """Refuse to delete a root Watch whose source has fragment Watches."""
      page = await client.list_info_sources(parent_info_source_id=str(root_watch.info_source_id))
      fragment_ids = [str(f.info_source_id) for f in page.items]
      if not fragment_ids:
          return
      result = await session.execute(
          select(Watch.id, Watch.info_source_id)
          .where(Watch.info_source_id.in_(fragment_ids))
          .where(Watch.is_archived.is_(False))
      )
      dependents = [(str(wid), str(sid)) for wid, sid in result.all()]
      if dependents:
          raise FragmentDependentsExistError(
              f"root Watch has fragment dependents: {dependents}"
          )
  ```

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: root/fragment Watch invariants"
  ```

### Task 5.3: Wire invariants into API + dashboard mutation routes

**Files:**
- Modify: `src/api/routes/watches.py` (POST create + DELETE)
- Modify: `src/dashboard/routes.py` (if it has its own create/delete handlers — likely yes)
- Test: `tests/api/routes/test_watches.py`

**Steps:**

- [ ] **Step 1: Read existing create/delete handlers**

  ```bash
  grep -n "router.post.*watches\|router.delete.*watches\|router.delete.*watch\|def watch_create\|def watch_archive\|def watch_delete" \
    src/api/routes/watches.py src/dashboard/routes.py
  ```
  Capture each handler's signature.

- [ ] **Step 2: Write failing API tests**

  ```python
  # tests/api/routes/test_watches.py — append
  @pytest.mark.asyncio
  async def test_create_fragment_watch_rejects_without_root(client, monkeypatch, ...):
      # Arrange: stub archiver SDK to report frag_id is a fragment of root_id.
      response = await client.post(
          "/api/v1/watches",
          json={"name": "frag", "content_type": "html", "info_source_id": frag_id},
          headers=HEADERS,
      )
      assert response.status_code == 422
      assert response.json()["detail"]["kind"] == "domain"


  @pytest.mark.asyncio
  async def test_delete_root_watch_blocks_when_fragments_exist(client, ...):
      response = await client.delete(f"/api/v1/watches/{root_id}", headers=HEADERS)
      assert response.status_code == 409


  @pytest.mark.asyncio
  async def test_delete_root_watch_cascade_archives_fragments(client, ...):
      response = await client.delete(
          f"/api/v1/watches/{root_id}?cascade=true", headers=HEADERS
      )
      assert response.status_code == 204
      # The fragment Watch's is_archived is now True, is_active False.
  ```

- [ ] **Step 3: Run, confirm failure**

- [ ] **Step 4: Wire invariants**

  In `src/api/routes/watches.py`:
  - On `POST /watches`: after parsing body, call `require_root_watch_on_chain(session, client, info_source_id=body.info_source_id)`. On `RootWatchMissingError` → `raise_envelope(422, "domain", "fragment requires active root Watch", ...)`.
  - On `DELETE /watches/{id}`: load the Watch. If `?cascade=true`, archive all fragments (loop, set `is_archived=True`, `is_active=False`). Otherwise call `require_no_fragment_dependents`; on `FragmentDependentsExistError` → `raise_envelope(409, "conflict", "fragment Watches depend on this root", data={"dependents": ...})`.
  - Mirror in `src/dashboard/routes.py` mutation handlers if they exist independently.

- [ ] **Step 5: Run + pass**

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: enforce fragment-root invariants on Watch create/delete"
  ```

### Task 5.4: Migration script (manifest-driven)

**Files:**
- Create: `scripts/migrate_watches_to_v2.py`
- Test: `tests/scripts/test_migrate_watches_to_v2.py`

**Steps:**

- [ ] **Step 1: Write failing script tests**

  ```python
  # tests/scripts/test_migrate_watches_to_v2.py
  """Tests for scripts/migrate_watches_to_v2.py."""
  import json

  import pytest

  from scripts.migrate_watches_to_v2 import MissingMappingError, migrate_watches
  from src.core.models.watch import Watch
  from tests.conftest import make_info_item, make_watch

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_happy_path_assigns_info_source_id(db_session):
      item = await make_info_item(db_session, name="X")
      watch = await make_watch(db_session, info_item_id=item.info_item_id)
      manifest = {str(item.info_item_id): "01HZZ00000000000000000ROOT"}
      await migrate_watches(db_session, manifest)
      await db_session.refresh(watch)
      assert str(watch.info_source_id) == "01HZZ00000000000000000ROOT"


  @pytest.mark.asyncio
  async def test_hard_errors_on_missing_mapping(db_session):
      item = await make_info_item(db_session, name="Orphan")
      watch = await make_watch(db_session, info_item_id=item.info_item_id)
      with pytest.raises(MissingMappingError) as exc:
          await migrate_watches(db_session, manifest={})
      assert str(item.info_item_id) in str(exc.value)


  @pytest.mark.asyncio
  async def test_idempotent_re_run(db_session):
      """Re-running over already-migrated Watches is a no-op."""
      item = await make_info_item(db_session, name="Y")
      watch = await make_watch(
          db_session, info_item_id=item.info_item_id,
          info_source_id="01HZZ00000000000000000ROOT",
      )
      await migrate_watches(db_session, manifest={})  # empty, no work to do
      await db_session.refresh(watch)
      assert str(watch.info_source_id) == "01HZZ00000000000000000ROOT"
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Write the script**

  ```python
  # scripts/migrate_watches_to_v2.py
  """One-shot: assign Watch.info_source_id from a manifest mapping.

  Operator pre-wires `information.info_item_sources` in Archiver, then
  supplies a JSON file mapping `info_item_id → info_source_id`. Script
  reads the manifest, applies to every Watch with NULL info_source_id,
  hard-errors on missing mappings.

  Usage:
    uv run python scripts/migrate_watches_to_v2.py --manifest watches.json

  Manifest format:
    {"01HZZ...ITEM_A": "01HZZ...SOURCE_A", "01HZZ...ITEM_B": "01HZZ...SOURCE_B"}
  """
  import argparse
  import asyncio
  import json
  import os
  import sys

  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

  from src.core.models.watch import Watch


  class MissingMappingError(Exception):
      """A Watch's info_item_id has no entry in the manifest."""


  async def migrate_watches(session, manifest: dict[str, str]) -> None:
      """Set Watch.info_source_id for each Watch with a NULL info_source_id."""
      result = await session.execute(
          select(Watch).where(Watch.info_source_id.is_(None))
      )
      watches = list(result.scalars().all())
      missing = []
      for w in watches:
          mapped = manifest.get(str(w.info_item_id))
          if mapped is None:
              missing.append((str(w.id), str(w.info_item_id)))
              continue
          w.info_source_id = mapped
      if missing:
          raise MissingMappingError(
              f"manifest missing mappings for: {missing}. "
              "Add entries and re-run."
          )
      await session.commit()


  async def _main():
      parser = argparse.ArgumentParser()
      parser.add_argument("--manifest", required=True,
                          help="Path to a JSON file mapping info_item_id → info_source_id")
      args = parser.parse_args()

      with open(args.manifest) as f:
          manifest = json.load(f)

      database_url = os.environ["DATABASE_URL"]
      engine = create_async_engine(database_url)
      factory = async_sessionmaker(engine, expire_on_commit=False)
      async with factory() as session:
          try:
              await migrate_watches(session, manifest)
              print("OK: all Watches assigned info_source_id")
          except MissingMappingError as e:
              print(f"FAIL: {e}", file=sys.stderr)
              sys.exit(1)


  if __name__ == "__main__":
      asyncio.run(_main())
  ```

- [ ] **Step 4: Run, confirm pass**

  ```bash
  uv run pytest tests/scripts/test_migrate_watches_to_v2.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: manifest-driven Watch.info_source_id migration script"
  ```

- [ ] **Step 6: Run against dev DB**

  Operator writes `watches.json` after wiring `info_item_sources` rows in Archiver:
  ```bash
  cat watches.json
  # {"01HZZ...ITEM_A": "01HZZ...SOURCE_A", ...}
  uv run python scripts/migrate_watches_to_v2.py --manifest watches.json
  ```
  Expected: `OK: all Watches assigned info_source_id`.

### Task 5.5: Make `info_source_id` NOT NULL, drop `info_item_id`

**Files:**
- Create: `alembic/versions/<rev>_drop_watch_info_item_id_make_info_source_not_null.py`
- Modify: `src/core/models/watch.py`
- Test: `tests/core/models/test_watch.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

  ```python
  @pytest.mark.asyncio
  async def test_watch_info_source_id_required(db_session):
      from sqlalchemy.exc import IntegrityError
      watch = Watch(name="T", content_type=ContentType.HTML)
      db_session.add(watch)
      with pytest.raises(IntegrityError):
          await db_session.flush()


  def test_watch_no_longer_has_info_item_id():
      assert not hasattr(Watch, "info_item_id")
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Update model**

  Before dropping the `info_items` stub, confirm no other model still FKs it:
  ```bash
  grep -rn 'information.info_items\|"info_items"' src/core/models/ 2>&1 | grep -v ".pyc"
  ```
  Expected: only the stub itself + watch.py. If anything else FKs `information.info_items`, keep the stub.

  In `src/core/models/watch.py`:
  - Drop the `info_item_id` mapped column.
  - Drop the `Table("info_items", …)` cross-schema stub (still keep `info_sources` stub).
  - Make `info_source_id` non-nullable:
    ```python
    info_source_id: Mapped[ULID] = mapped_column(
        ULIDType,
        ForeignKey("information.info_sources.info_source_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ```

- [ ] **Step 4: Generate migration**

  ```bash
  uv run alembic revision --autogenerate -m "drop watches.info_item_id, make info_source_id not null"
  ```

  Hand-verify: drops FK to `information.info_items`, drops the column, alters `info_source_id` NOT NULL.

- [ ] **Step 5: Apply + tests**

  ```bash
  uv run alembic upgrade head
  uv run pytest tests/core/models/test_watch.py --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 6: Sweep callers of `Watch.info_item_id`**

  ```bash
  grep -rn "\.info_item_id\|Watch.info_item_id\|watch.info_item_id" src/ tests/ tools/ scripts/ 2>&1 | grep -v ".pyc" | head -20
  ```
  Each remaining site needs to migrate to `info_source_id` (or be removed). Most get touched in Stage 7 (pipeline) and Stage 10 (deletions).

- [ ] **Step 7: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: drop watches.info_item_id, info_source_id NOT NULL"
  ```

### Task 5.6: Cadence reconciliation

**Files:**
- Create: `src/core/watches/cadence.py`
- Test: `tests/core/watches/test_cadence.py`

The wiring into the actual scheduler lives in Task 7.1 (when the pipeline rewrite touches `check_watch`).

**Steps:**

- [ ] **Step 1: Write failing test**

  ```python
  # tests/core/watches/test_cadence.py
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from src.core.watches.cadence import effective_root_cadence_seconds
  from tests.conftest import make_watch

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_min_of_root_and_fragment_schedules(db_session):
      root = await make_watch(
          db_session,
          info_source_id="01HZZ00000000000000000ROOT",
          schedule_config={"interval_seconds": 3600},
      )
      await make_watch(
          db_session,
          info_source_id="01HZZ00000000000000FRAG1",
          schedule_config={"interval_seconds": 900},
      )
      await make_watch(
          db_session,
          info_source_id="01HZZ00000000000000FRAG2",
          schedule_config={"interval_seconds": 600},
      )
      client = MagicMock()
      client.list_info_sources = AsyncMock(return_value=MagicMock(items=[
          MagicMock(info_source_id="01HZZ00000000000000FRAG1"),
          MagicMock(info_source_id="01HZZ00000000000000FRAG2"),
      ]))
      seconds = await effective_root_cadence_seconds(db_session, client, root)
      assert seconds == 600
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Write the module**

  ```python
  # src/core/watches/cadence.py
  """Effective root cadence = min(root.schedule, min(fragment_schedules))."""
  from archiver_client import ArchiverClient
  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import AsyncSession

  from src.core.models.watch import Watch


  async def effective_root_cadence_seconds(
      session: AsyncSession,
      client: ArchiverClient,
      root_watch: Watch,
  ) -> int:
      """Return min(root.interval, min(fragment.intervals))."""
      page = await client.list_info_sources(
          parent_info_source_id=str(root_watch.info_source_id)
      )
      frag_ids = [str(f.info_source_id) for f in page.items]
      intervals = [int(root_watch.schedule_config.get("interval_seconds", 3600))]
      if frag_ids:
          result = await session.execute(
              select(Watch.schedule_config)
              .where(Watch.info_source_id.in_(frag_ids))
              .where(Watch.is_active.is_(True))
              .where(Watch.is_archived.is_(False))
          )
          for (cfg,) in result.all():
              intervals.append(int(cfg.get("interval_seconds", 3600)))
      return min(intervals)
  ```

- [ ] **Step 4: Run + pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: derived root cadence = min(root, fragment) schedules"
  ```

---

## Stage 6 — Scratch-file allocator

### Task 6.1: ULID allocation, write, rename

**Files:**
- Create: `src/core/sources/scratch.py`
- Test: `tests/core/sources/test_scratch.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

  ```python
  # tests/core/sources/test_scratch.py
  from src.core.sources.scratch import (
      allocate_revision_id,
      rename_scratch_to_canonical,
      scratch_path_for,
      write_scratch_bytes,
  )


  def test_allocate_revision_id_returns_ulid_string():
      uid = allocate_revision_id()
      assert isinstance(uid, str)
      assert len(uid) == 26


  def test_write_scratch_bytes_round_trips(tmp_path, monkeypatch):
      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      uid = allocate_revision_id()
      path = write_scratch_bytes(uid, b"hello world")
      assert path.exists()
      assert path.read_bytes() == b"hello world"
      assert path.name == f"{uid}.bin"


  def test_rename_to_canonical_when_ids_differ(tmp_path, monkeypatch):
      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      old_uid = allocate_revision_id()
      new_uid = allocate_revision_id()
      old_path = write_scratch_bytes(old_uid, b"data")
      new_path = rename_scratch_to_canonical(old_uid, new_uid)
      assert not old_path.exists()
      assert new_path.exists()
      assert new_path.name == f"{new_uid}.bin"


  def test_rename_noop_when_ids_match(tmp_path, monkeypatch):
      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      uid = allocate_revision_id()
      original = write_scratch_bytes(uid, b"data")
      returned = rename_scratch_to_canonical(uid, uid)
      assert returned == original
      assert returned.exists()
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Write the module**

  ```python
  # src/core/sources/scratch.py
  """Scratch-file management for SourceRevision content."""
  import os
  from pathlib import Path

  from ulid import ULID

  DEFAULT_CACHE_DIR = "/var/cache/watcher/scratch"


  def _cache_dir() -> Path:
      d = Path(os.environ.get("WATCHER_CACHE_DIR", DEFAULT_CACHE_DIR))
      d.mkdir(parents=True, exist_ok=True)
      return d


  def allocate_revision_id() -> str:
      """Return a fresh ULID string suitable for `source_revision_id`."""
      return str(ULID())


  def scratch_path_for(revision_id: str) -> Path:
      return _cache_dir() / f"{revision_id}.bin"


  def write_scratch_bytes(revision_id: str, content: bytes) -> Path:
      """Write content to <cache_dir>/<revision_id>.bin atomically."""
      target = scratch_path_for(revision_id)
      tmp = target.with_suffix(".bin.tmp")
      tmp.write_bytes(content)
      tmp.replace(target)
      return target


  def rename_scratch_to_canonical(allocated_id: str, canonical_id: str) -> Path:
      """Rename allocated → canonical when server returned a different ULID."""
      if allocated_id == canonical_id:
          return scratch_path_for(canonical_id)
      source = scratch_path_for(allocated_id)
      target = scratch_path_for(canonical_id)
      if target.exists():
          source.unlink(missing_ok=True)
          return target
      source.rename(target)
      return target
  ```

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: scratch-file allocator + writer + rename safety net"
  ```

---

## Stage 7 — Pipeline rewrite

### Task 7.1: Resolve URL through new resolver; thread effective domain

**Files:**
- Modify: `src/workers/tasks.py`
- Modify: `src/workers/pipeline.py` (signature only)
- Test: `tests/workers/test_tasks.py`

**Steps:**

- [ ] **Step 1: Read existing `check_watch` carefully**

  ```bash
  uv run python -c "import inspect; from src.workers import tasks; print(inspect.getsource(tasks.check_watch))" | head -80
  ```
  Capture the existing flow: how it loads the Watch, calls `resolve_primary`, threads the URL.

- [ ] **Step 2: Write failing test**

  Append to `tests/workers/test_tasks.py`:
  ```python
  @pytest.mark.asyncio
  async def test_check_watch_uses_resolve_root_sources(db_session, monkeypatch):
      """check_watch resolves via resolve_root_sources_with_children."""
      from unittest.mock import AsyncMock, MagicMock, patch

      from src.core.sources.resolver import ResolvedRootSource

      watch = await make_watch(db_session, info_source_id="01HZZ00000000000000000ROOT")
      await db_session.commit()

      resolved = ResolvedRootSource(
          info_source_id="01HZZ00000000000000000ROOT",
          url="https://example.com",
          source_spec={"target": {"url": "https://example.com"}},
          children=[],
      )
      with patch("src.workers.tasks.resolve_root_sources_with_children",
                 new=AsyncMock(return_value=resolved)) as mock_resolve:
          ...  # invoke check_watch with mocked fetcher + storage
      mock_resolve.assert_awaited_once()
  ```

- [ ] **Step 3: Refactor `check_watch`**

  In `src/workers/tasks.py`:
  - Replace `from src.core.info_resolver import resolve_primary, ResolvedInfoSpec` with `from src.core.sources.resolver import resolve_root_sources_with_children, ResolvedRootSource`.
  - Update the resolver call: `resolved = await resolve_root_sources_with_children(info_client, str(watch.info_source_id))`.
  - Replace `fetch_url = resolved.document["target"]["url"]` with `fetch_url = resolved.url`.
  - All current `resolved.document` references switch to `resolved.source_spec`.
  - Update the call into the pipeline:
    ```python
    await _run_check_pipeline(..., resolved=resolved, info_client=info_client)
    ```

- [ ] **Step 4: Update pipeline signature only**

  In `src/workers/pipeline.py`, change `_run_check_pipeline(..., resolved: ResolvedInfoSpec)` → `_run_check_pipeline(..., resolved: ResolvedRootSource)`. Inside the function body, switch `resolved.document` → `resolved.source_spec` and `resolved.info_item_id` → leave for the rewrite in Task 7.2. The body still uses Snapshot — that's OK, Stage 7.2 replaces it.

- [ ] **Step 5: Wire cadence into the periodic scheduling path**

  Find how `check_watch` decides its next-tick. If Procrastinate's `@bp.periodic` is per-watch, the cadence reconciler from Task 5.6 runs *before* scheduling. Likely the periodic-task setup happens in `src/workers/tasks.py:schedule_tick` (read it):
  ```bash
  grep -n "schedule_tick\|periodic\|interval_seconds" src/workers/tasks.py
  ```
  Add a call to `effective_root_cadence_seconds(session, client, watch)` when re-scheduling root Watches. If this requires deeper integration with how the existing scheduler dispatches per-watch periodic invocations, capture as a follow-up and call it out in this task's commit — don't block the stage on perfect scheduler integration.

- [ ] **Step 6: Run targeted tests**

  ```bash
  uv run pytest tests/workers/test_tasks.py --no-cov 2>&1 | tail -10
  ```
  Existing pipeline tests will fail until Task 7.2 lands.

- [ ] **Step 7: Commit (with known-failing pipeline tests)**

  ```bash
  git add src/workers/tasks.py src/workers/pipeline.py tests/workers/test_tasks.py
  git commit -m "#156 refactor: check_watch uses resolve_root_sources_with_children + cadence reconciliation"
  ```

### Task 7.2: Pipeline — root POST with client-allocated ULID + fast-path

**Files:**
- Modify: `src/workers/pipeline.py`
- Test: `tests/workers/test_pipeline.py`

**Steps:**

- [ ] **Step 1: Write failing test**

  ```python
  # tests/workers/test_pipeline.py — replace prior tests scoped to the rewrite
  @pytest.mark.asyncio
  async def test_pipeline_writes_scratch_then_posts_root_revision(
      db_session, tmp_path, monkeypatch
  ):
      from unittest.mock import AsyncMock, MagicMock

      from src.core.sources.resolver import ResolvedRootSource
      from src.workers.pipeline import _run_check_pipeline

      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "600")

      watch = await make_watch(db_session, info_source_id="01HZZ00000000000000000ROOT")
      resolved = ResolvedRootSource(
          info_source_id="01HZZ00000000000000000ROOT",
          url="https://example.com",
          source_spec={
              "target": {"url": "https://example.com"},
              "extraction": {"algorithm": "full_page"},
          },
          children=[],
      )
      fake_client = MagicMock()
      fake_client.post_source_revision = AsyncMock(return_value=MagicMock(
          source_revision_id="01HZZ00000000000000000REV",
          content_fingerprint="sha256:" + "a" * 64,
      ))

      result = await _run_check_pipeline(
          watch=watch,
          raw_content=b"<html>body</html>",
          fetcher_used="http",
          fetch_duration_ms=10,
          session=db_session,
          resolved=resolved,
          info_client=fake_client,
      )
      assert len(list(tmp_path.glob("*.bin"))) == 1
      kwargs = fake_client.post_source_revision.await_args.kwargs
      assert kwargs["source_revision_id"]
      assert kwargs["content_cache_uri"].startswith("file:///")
      assert kwargs["content_fingerprint"].startswith("sha256:")
      assert result["is_changed"] is True


  @pytest.mark.asyncio
  async def test_pipeline_fast_path_skips_post_when_fingerprint_matches(
      db_session, tmp_path, monkeypatch
  ):
      """When last_known_revisions has the same fingerprint, no POST."""
      from src.core.sources.revision_cache import upsert_last_known
      from src.workers.pipeline import _run_check_pipeline
      ... # seed last_known_revisions with the fingerprint of b"<html>body</html>"

      fake_client = MagicMock()
      fake_client.post_source_revision = AsyncMock()
      result = await _run_check_pipeline(...)
      fake_client.post_source_revision.assert_not_awaited()
      assert result.get("skipped_reason") == "fast_path"


  @pytest.mark.asyncio
  async def test_pipeline_outboxes_when_archiver_unreachable(...):
      """ConnectError → row in pending_source_revisions; no cascade attempted."""
      import httpx
      fake_client = MagicMock()
      fake_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("nope"))
      ...
      result = await _run_check_pipeline(...)
      assert result.get("outbox") is True
      from sqlalchemy import select
      from src.core.models.pending_source_revision import PendingSourceRevision
      pending = (await db_session.execute(select(PendingSourceRevision))).scalars().all()
      assert len(pending) == 1
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Rewrite the pipeline (root portion)**

  Replace the body of `_run_check_pipeline` in `src/workers/pipeline.py`:

  ```python
  import hashlib
  from datetime import UTC, datetime, timedelta
  from os import environ

  from src.core.notifications.events import WatchEvent, WatchEventType
  from src.core.notifications.notify import dispatch_event_notifications
  from src.core.sources.outbox import enqueue_pending
  from src.core.sources.resolver import ResolvedRootSource
  from src.core.sources.revision_cache import get_last_fingerprint, upsert_last_known
  from src.core.sources.scratch import (
      allocate_revision_id,
      rename_scratch_to_canonical,
      scratch_path_for,
      write_scratch_bytes,
  )

  WATCHER_CACHE_TTL_SECONDS = int(environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


  async def _run_check_pipeline(
      watch,
      raw_content: bytes,
      fetcher_used: str,
      fetch_duration_ms: int,
      session,
      *,
      resolved: ResolvedRootSource,
      info_client,
  ) -> dict:
      """Fetch → scratch → POST root → cascade. Outbox on POST failure."""
      # 1. Extract root content per source_spec.
      root_extracted = await _extract_with_spec(raw_content, resolved.source_spec)
      root_bytes = root_extracted.canonical_bytes  # post-trim, UTF-8

      # 2. SHA-256.
      fingerprint = "sha256:" + hashlib.sha256(root_bytes).hexdigest()

      # 3. Fast-path: local cache lookup, no Archiver round-trip.
      prior_fp = await get_last_fingerprint(session, resolved.info_source_id)
      if prior_fp == fingerprint:
          return {"is_changed": False, "skipped_reason": "fast_path"}

      # 4. Allocate ULID, write scratch.
      allocated_id = allocate_revision_id()
      scratch_path = write_scratch_bytes(allocated_id, root_bytes)
      cache_uri = f"file://{scratch_path}"
      now = datetime.now(UTC)
      expires_at = now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS)

      # 5. POST.
      try:
          response = await info_client.post_source_revision(
              info_source_id=resolved.info_source_id,
              content_fingerprint=fingerprint,
              captured_at=now,
              source_revision_id=allocated_id,
              content_cache_uri=cache_uri,
              content_cache_expires_at=expires_at,
              content_size_bytes=len(root_bytes),
              content_media_type=root_extracted.media_type,
          )
      except Exception as e:
          # Outbox path. Cascade aborts: it needs root revision to exist in
          # Archiver to attach the fragment's binding semantics later.
          await enqueue_pending(
              session,
              info_source_id=resolved.info_source_id,
              content_fingerprint=fingerprint,
              captured_at=now,
              content_cache_uri=cache_uri,
              content_cache_expires_at=expires_at,
              content_size_bytes=len(root_bytes),
              content_media_type=root_extracted.media_type,
          )
          return {"is_changed": True, "outbox": True, "error": str(e)}

      # 6. Idempotency reconcile (rare).
      canonical_id = str(response.source_revision_id)
      if canonical_id != allocated_id:
          rename_scratch_to_canonical(allocated_id, canonical_id)

      # 7. Update local cache.
      await upsert_last_known(
          session,
          info_source_id=resolved.info_source_id,
          content_fingerprint=fingerprint,
          source_revision_id=canonical_id,
          captured_at=now,
      )

      # 8. Dispatch via existing WatchEvent path.
      event = WatchEvent(
          event_type=WatchEventType.CHANGE_DETECTED,
          watch_id=str(watch.id),
          watch_name=watch.name,
          watch_url=watch.effective_url or resolved.url,
          occurred_at=now,
          metadata={
              "source_revision_id": canonical_id,
              "info_source_id": resolved.info_source_id,
              "content_fingerprint": fingerprint,
          },
      )
      await dispatch_event_notifications(session, event)

      result = {
          "is_changed": True,
          "source_revision_id": canonical_id,
          "scratch_path": str(scratch_path_for(canonical_id)),
      }
      # Cascade lives in Task 7.3 — appends to `result`.
      return result
  ```

  **Note on `_extract_with_spec`.** Today's pipeline has an extraction helper that takes `(raw_content, spec_document)`. Keep using the same helper; just pass `resolved.source_spec`. If the helper signature differs (e.g., expected `document` not `source_spec`), update the helper to accept either.

  **Note on `canonical_bytes` / `media_type`.** Today's extractor returns `chunks` + `text_bytes`. Adapt: the root SourceRevision needs a single bytes blob (post-extraction, post-trim). If the helper doesn't produce one, compose it (`b"\n".join(chunk.bytes for chunk in chunks)`).

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: pipeline writes scratch + POSTs root SourceRevision + outbox fallback"
  ```

### Task 7.3: Pipeline — fragment cascade

**Files:**
- Modify: `src/workers/pipeline.py`
- Test: `tests/workers/test_pipeline.py`

**Steps:**

- [ ] **Step 1: Write failing test**

  ```python
  @pytest.mark.asyncio
  async def test_pipeline_cascades_fragments_from_cached_bytes(...):
      resolved = ResolvedRootSource(
          ...,
          children=[
              ResolvedFragmentSource(
                  info_source_id="01HZZ00000000000000FRAG1",
                  parent_info_source_id="01HZZ00000000000000000ROOT",
                  source_spec={"extraction": {"algorithm": "css", "selector": "#x"}},
              ),
              ResolvedFragmentSource(
                  info_source_id="01HZZ00000000000000FRAG2",
                  parent_info_source_id="01HZZ00000000000000000ROOT",
                  source_spec={"extraction": {"algorithm": "css", "selector": "#y"}},
              ),
          ],
      )
      ...
      # Assert: 1 root POST + 2 fragment POSTs.
      assert fake_client.post_source_revision.await_count == 3
      assert len(list(tmp_path.glob("*.bin"))) == 3
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Extend pipeline with cascade**

  Append after the root dispatch block in `_run_check_pipeline`:

  ```python
  # 9. Cascade: extract each fragment from the SAME raw_content.
  fragment_revision_ids = []
  for fragment in resolved.children:
      frag_extracted = await _extract_with_spec(raw_content, fragment.source_spec)
      frag_bytes = frag_extracted.canonical_bytes
      frag_fingerprint = "sha256:" + hashlib.sha256(frag_bytes).hexdigest()

      # Per-fragment fast-path.
      prior_frag_fp = await get_last_fingerprint(session, fragment.info_source_id)
      if prior_frag_fp == frag_fingerprint:
          continue

      frag_allocated_id = allocate_revision_id()
      frag_scratch_path = write_scratch_bytes(frag_allocated_id, frag_bytes)
      frag_cache_uri = f"file://{frag_scratch_path}"
      frag_now = datetime.now(UTC)

      try:
          frag_response = await info_client.post_source_revision(
              info_source_id=fragment.info_source_id,
              content_fingerprint=frag_fingerprint,
              captured_at=frag_now,
              source_revision_id=frag_allocated_id,
              content_cache_uri=frag_cache_uri,
              content_cache_expires_at=frag_now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS),
              content_size_bytes=len(frag_bytes),
              content_media_type=frag_extracted.media_type,
          )
      except Exception as e:
          await enqueue_pending(
              session,
              info_source_id=fragment.info_source_id,
              content_fingerprint=frag_fingerprint,
              captured_at=frag_now,
              content_cache_uri=frag_cache_uri,
              content_cache_expires_at=frag_now + timedelta(seconds=WATCHER_CACHE_TTL_SECONDS),
              content_size_bytes=len(frag_bytes),
              content_media_type=frag_extracted.media_type,
          )
          continue

      frag_canonical_id = str(frag_response.source_revision_id)
      if frag_canonical_id != frag_allocated_id:
          rename_scratch_to_canonical(frag_allocated_id, frag_canonical_id)

      await upsert_last_known(
          session,
          info_source_id=fragment.info_source_id,
          content_fingerprint=frag_fingerprint,
          source_revision_id=frag_canonical_id,
          captured_at=frag_now,
      )

      # Dispatch per-fragment Watch (if one exists).
      frag_watch = await session.execute(
          select(Watch).where(
              Watch.info_source_id == fragment.info_source_id,
              Watch.is_active.is_(True),
              Watch.is_archived.is_(False),
          )
      )
      frag_watch_row = frag_watch.scalar_one_or_none()
      if frag_watch_row is not None:
          await dispatch_event_notifications(session, WatchEvent(
              event_type=WatchEventType.CHANGE_DETECTED,
              watch_id=str(frag_watch_row.id),
              watch_name=frag_watch_row.name,
              watch_url=frag_watch_row.effective_url or resolved.url,
              occurred_at=frag_now,
              metadata={
                  "source_revision_id": frag_canonical_id,
                  "info_source_id": fragment.info_source_id,
                  "content_fingerprint": frag_fingerprint,
                  "is_fragment": True,
                  "parent_info_source_id": fragment.parent_info_source_id,
              },
          ))

      fragment_revision_ids.append(frag_canonical_id)

  result["fragment_revision_ids"] = fragment_revision_ids
  ```

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: pipeline cascade — fragments extract from cached root bytes"
  ```

### Task 7.4: Delete `src/core/info_resolver.py`

Once Task 7.1 + 7.2 landed all callers of the old resolver, delete it.

**Files:**
- Delete: `src/core/info_resolver.py`, `tests/core/test_info_resolver.py`

**Steps:**

- [ ] **Step 1: Confirm no remaining importers**

  ```bash
  grep -rn "from src.core.info_resolver\|resolve_primary\|ResolvedInfoSpec" src/ tests/ scripts/ tools/ 2>&1 | grep -v ".pyc"
  ```
  Expected: empty.

- [ ] **Step 2: Delete**

  ```bash
  git rm src/core/info_resolver.py tests/core/test_info_resolver.py
  ```

- [ ] **Step 3: Run full suite**

  ```bash
  uv run pytest --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: delete src/core/info_resolver.py (replaced by sources/resolver.py)"
  ```

---

## Stage 8 — Sweeper with outbox interlock

### Task 8.1: Sweeper periodic task

**Files:**
- Create: `src/workers/cache_sweeper.py`
- Test: `tests/workers/test_cache_sweeper.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

  ```python
  # tests/workers/test_cache_sweeper.py
  import os
  from datetime import UTC, datetime, timedelta

  import pytest
  from ulid import ULID

  from src.core.models.pending_source_revision import PendingSourceRevision
  from src.workers.cache_sweeper import sweep_scratch_cache

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_sweeper_deletes_files_older_than_ttl(tmp_path, monkeypatch, db_session):
      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")
      old = tmp_path / "01HZZ00000000000000000OLD.bin"
      young = tmp_path / "01HZZ00000000000000000NEW.bin"
      old.write_bytes(b"old")
      young.write_bytes(b"new")
      old_mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
      os.utime(old, (old_mtime, old_mtime))

      result = await sweep_scratch_cache()
      assert result["deleted"] == 1
      assert not old.exists()
      assert young.exists()


  @pytest.mark.asyncio
  async def test_sweeper_skips_files_in_outbox(tmp_path, monkeypatch, db_session):
      monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
      monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "60")
      reserved = "01HZZ00000000000000000RES"
      f = tmp_path / f"{reserved}.bin"
      f.write_bytes(b"reserved")
      mtime = (datetime.now(UTC) - timedelta(seconds=120)).timestamp()
      os.utime(f, (mtime, mtime))
      now = datetime.now(UTC)
      row = PendingSourceRevision(
          id=ULID.from_str(reserved),
          info_source_id="01HZZ00000000000000000000F",
          content_fingerprint="sha256:" + "a" * 64,
          captured_at=now,
          content_cache_uri=f"file://{f}",
          content_cache_expires_at=now,
          next_attempt_at=now,
      )
      db_session.add(row)
      await db_session.commit()
      result = await sweep_scratch_cache()
      assert result["deleted"] == 0
      assert result["skipped"] == 1
      assert f.exists()
  ```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Write the sweeper**

  ```python
  # src/workers/cache_sweeper.py
  """Periodic task: delete stale scratch files + PATCH-cache-clear."""
  import os
  import re
  from datetime import UTC, datetime, timedelta
  from pathlib import Path

  from sqlalchemy import select
  from ulid import ULID

  from src.core.database import get_session_factory
  from src.core.logging import get_logger
  from src.core.models.pending_source_revision import PendingSourceRevision
  from src.core.registry import get_registry
  from src.workers import bp

  logger = get_logger(__name__)

  _ULID_FILENAME = re.compile(r"^([0-9A-HJKMNP-TV-Z]{26})\.bin$")


  def _cache_dir() -> Path:
      return Path(os.environ.get("WATCHER_CACHE_DIR", "/var/cache/watcher/scratch"))


  def _ttl_seconds() -> int:
      return int(os.environ.get("WATCHER_CACHE_TTL_SECONDS", "600"))


  @bp.periodic(cron="* * * * *", periodic_id="sweep_scratch_cache")
  @bp.task(name="sweep_scratch_cache", queue="default")
  async def sweep_scratch_cache(**periodic_kwargs) -> dict:
      cutoff = datetime.now(UTC) - timedelta(seconds=_ttl_seconds())
      cache_dir = _cache_dir()
      if not cache_dir.exists():
          return {"deleted": 0, "skipped": 0, "patch_failures": 0}

      candidates: list[tuple[str, Path]] = []
      for p in cache_dir.iterdir():
          if not p.is_file():
              continue
          m = _ULID_FILENAME.match(p.name)
          if not m:
              continue
          mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
          if mtime > cutoff:
              continue
          candidates.append((m.group(1), p))

      if not candidates:
          return {"deleted": 0, "skipped": 0, "patch_failures": 0}

      candidate_ulids = [ULID.from_str(rid) for rid, _ in candidates]
      factory = get_session_factory()
      async with factory() as session:
          result = await session.execute(
              select(PendingSourceRevision.id).where(PendingSourceRevision.id.in_(candidate_ulids))
          )
          reserved = {str(rid) for (rid,) in result.all()}

      deleted = 0
      skipped = 0
      patch_failures = 0
      client = get_registry().get_archiver_client()
      for revision_id, path in candidates:
          if revision_id in reserved:
              skipped += 1
              continue
          try:
              path.unlink()
          except OSError as e:
              logger.warning("scratch delete failed", extra={"path": str(path), "error": str(e)})
              continue
          deleted += 1
          try:
              await client.patch_source_revision_cache(
                  revision_id,
                  content_cache_uri=None,
                  content_cache_expires_at=None,
              )
          except Exception as e:
              patch_failures += 1
              logger.warning(
                  "patch cache-clear failed",
                  extra={"revision_id": revision_id, "error": str(e)},
              )

      logger.info(
          "sweep_scratch_cache finished",
          extra={"deleted": deleted, "skipped": skipped, "patch_failures": patch_failures},
      )
      return {"deleted": deleted, "skipped": skipped, "patch_failures": patch_failures}
  ```

- [ ] **Step 4: Run, confirm pass**

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 feat: scratch-cache sweeper with outbox interlock"
  ```

---

## Stage 9 — Notification dispatch cutover

The pipeline + drain already emit `WatchEvent.CHANGE_DETECTED` with `source_revision_id` in metadata. This stage updates `notify.py` to:
- Drop the Snapshot-based unified-diff path (`_load_event_unified_diff`).
- Add fragment-aware template variable resolution (so `{{ watch.url }}` resolves to the chain's root URL on a fragment Watch).

### Task 9.1: Drop `_load_event_unified_diff` from notify.py

After Phase 5, Snapshot + Change tables are going away — the unified diff cannot be computed. Notifications fall back to fingerprint-shift-only (page-level "this changed").

**Files:**
- Modify: `src/core/notifications/notify.py`
- Test: `tests/core/notifications/test_notify.py`

**Steps:**

- [ ] **Step 1: Write failing test**

  ```python
  # tests/core/notifications/test_notify.py — append
  @pytest.mark.asyncio
  async def test_dispatch_change_detected_renders_without_diff(db_session, monkeypatch):
      """A CHANGE_DETECTED event with no Snapshot/Change still dispatches with empty diff."""
      from datetime import UTC, datetime
      from src.core.notifications.events import WatchEvent, WatchEventType
      from src.core.notifications.notify import dispatch_event_notifications

      # Set up: an active Watch + a domain-level template that references {{ unified_diff }}.
      watch = await make_watch(db_session, name="W", info_source_id="01HZZ...ROOT")
      ...  # seed NotificationTemplate referencing unified_diff via body_template

      event = WatchEvent(
          event_type=WatchEventType.CHANGE_DETECTED,
          watch_id=str(watch.id),
          watch_name=watch.name,
          watch_url=watch.effective_url or "https://example.com",
          occurred_at=datetime.now(UTC),
          metadata={
              "source_revision_id": "01HZZ...REV",
              "info_source_id": "01HZZ...ROOT",
              "content_fingerprint": "sha256:" + "a" * 64,
          },
      )
      # Patch the Notifier SDK call to a no-op.
      ...
      await dispatch_event_notifications(db_session, event)
      # No raise; unified_diff renders as empty string.
  ```

- [ ] **Step 2: Run, confirm failure (or unexpected behavior)**

- [ ] **Step 3: Edit `src/core/notifications/notify.py`**

  - Delete the `_load_event_unified_diff`, `_load_text_pair` functions, and their callsites.
  - Replace the unified-diff rendering var with a constant empty string `""` at template render time (or unset it; whichever the template engine prefers).
  - Drop `from src.core.diff.normalize` and `from src.core.diff.textual` imports.
  - Drop `from src.core.models.change import Change` and `from src.core.models.snapshot import Snapshot`.
  - Update the `_candidate_needs_unified_diff` helper: since no diff source exists, the helper can return False unconditionally or be removed entirely. Removing it (and its caller) is cleaner.

  The function will still need a placeholder `unified_diff = ""` if downstream rendering accesses the variable. Confirm via:
  ```bash
  grep -rn "unified_diff\|diff_snippet\|diff_full" src/core/notifications/ 2>&1
  ```

- [ ] **Step 4: Update `dispatch_event_notifications` template context**

  Where the function previously called `_load_event_unified_diff(...)`, replace with `unified_diff = ""`. The template still gets the variable; it just renders empty.

- [ ] **Step 5: Run, confirm pass**

  ```bash
  uv run pytest tests/core/notifications/ --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop Snapshot-based unified-diff from notification rendering"
  ```

### Task 9.2: Fragment-aware template var resolution

For a fragment Watch, `{{ watch.url }}` should resolve to the chain's root URL (since fragments don't have their own URL).

**Files:**
- Modify: `src/core/notifications/notify.py` or the template-var resolver (find via `grep`)
- Test: `tests/core/notifications/test_fragment_vars.py`

**Steps:**

- [ ] **Step 1: Locate where `watch.url` is resolved for templates**

  ```bash
  grep -rn "watch.url\|watch\.effective_url" src/core/notifications/ 2>&1 | head -10
  ```
  Likely in `notify.py` or `src/core/notifications/content.py`.

- [ ] **Step 2: Write failing test**

  Test that a fragment Watch's notification template renders the root URL, not the empty fragment URL.

- [ ] **Step 3: Resolve URL via Archiver if Watch's source is a fragment**

  In the template-var resolver, if `event.metadata.get("is_fragment")`, walk to root via the SDK (cache result per dispatch):
  ```python
  if event.metadata.get("is_fragment"):
      parent_id = event.metadata.get("parent_info_source_id")
      if parent_id:
          root = await client.get_info_source(parent_id)
          url = root.source_spec.get("target", {}).get("url", "")
  ```

  Cleaner: cache the URL at Watch-create time in `effective_url` and let it ride through — Watch.effective_url is already root-resolved at creation per the design. So the change is **a no-op** in production. Confirm by inspecting Watch.effective_url's value for a fragment-Watch in dev.

- [ ] **Step 4: Run + pass**

- [ ] **Step 5: Commit (or skip if no-op)**

  ```bash
  git add -A
  git commit -m "#156 feat: fragment-aware template var resolution"
  ```

---

## Stage 10 — Deletions (per-surface)

Each task takes one surface to delete + its tests + reference points. Order matters: notify.py first (Stage 9), then routes/dashboard/context that read Snapshot/Change, finally the model/table drops.

### Task 10.1: Delete `src/api/routes/changes.py`

**Files:**
- Delete: `src/api/routes/changes.py`
- Delete: `tests/api/routes/test_changes.py` (if present)
- Modify: `src/api/main.py` — remove the `app.include_router(...)` line for this router.

**Steps:**

- [ ] **Step 1: Inspect the router setup**

  ```bash
  grep -n "include_router\|routes.changes\|src.api.routes.changes" src/api/main.py src/api/routes/__init__.py
  ```

- [ ] **Step 2: Confirm no other importers**

  ```bash
  grep -rn "from src.api.routes.changes\|api.routes.changes" src/ tests/ 2>&1 | grep -v ".pyc"
  ```

- [ ] **Step 3: Delete**

  ```bash
  git rm src/api/routes/changes.py
  git rm tests/api/routes/test_changes.py 2>/dev/null
  ```

- [ ] **Step 4: Remove the router from main.py**

  Delete the `app.include_router(...)` line that adds the changes router.

- [ ] **Step 5: Run pytest collection**

  ```bash
  uv run pytest --collect-only 2>&1 | tail -10
  ```
  Expected: clean (no import errors).

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: delete /api/v1/changes routes (Change table gone)"
  ```

### Task 10.2: Delete `Change` model + migration

**Files:**
- Delete: `src/core/models/change.py`
- Delete: `tests/core/models/test_change.py`
- Modify: `src/core/models/__init__.py` (remove Change re-export)
- Create: `alembic/versions/<rev>_drop_changes_table.py`
- Modify: `tests/conftest.py` (remove trigger recreation, remove `make_change` fixture)

**Steps:**

- [ ] **Step 1: Inspect Change consumers (should be ≤ Stage 9/Task 10.1 + 10.3)**

  ```bash
  grep -rn "Change\b\|from src.core.models.change" src/ tests/ 2>&1 | grep -v ".pyc" | head -20
  ```
  Each remaining site is in scope of this task or Task 10.3 (dashboard).

- [ ] **Step 2: Delete the model + tests + fixture**

  ```bash
  git rm src/core/models/change.py tests/core/models/test_change.py
  ```

  Remove `from src.core.models.change import Change` from `src/core/models/__init__.py`.

- [ ] **Step 3: Remove the trigger from `tests/conftest.py`**

  ```bash
  grep -n "trg_changes_update_last_changed_at\|update_watch_last_changed_at" tests/conftest.py
  ```
  Delete that block. Per AGENTS.md DB Triggers gotcha — Stage 10's migration drops the trigger; tests no longer need to recreate it.

- [ ] **Step 4: Remove `make_change` fixture**

  ```bash
  grep -n "make_change\|change_factory" tests/conftest.py
  ```
  Delete fixtures.

- [ ] **Step 5: Generate drop migration**

  ```bash
  uv run alembic revision --autogenerate -m "drop changes table + trigger"
  ```

  Hand-edit: the autogenerator may not drop the trigger. Add manually:
  ```python
  def upgrade():
      op.execute("DROP TRIGGER IF EXISTS trg_changes_update_last_changed_at ON changes")
      op.execute("DROP FUNCTION IF EXISTS update_watch_last_changed_at()")
      op.drop_table("changes")
  ```

- [ ] **Step 6: Apply + run tests**

  ```bash
  uv run alembic upgrade head
  uv run pytest --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 7: Update AGENTS.md DB Triggers section**

  Remove the "Current triggers: `trg_changes_update_last_changed_at`" line — no triggers exist post-cutover.

- [ ] **Step 8: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop Change table + last_changed_at trigger"
  ```

### Task 10.3: Trim Snapshot/Change pages from `src/dashboard/routes.py` and `context.py`

The dashboard has Snapshot detail pages (lines ~395-515) and Change detail pages (~2289). All gone.

**Files:**
- Modify: `src/dashboard/routes.py`
- Modify: `src/dashboard/context.py`
- Delete: templates rendering Snapshot/Change pages (find via grep)

**Steps:**

- [ ] **Step 1: Map the Snapshot/Change handlers in dashboard**

  ```bash
  grep -nE "snapshot|change_detail|/changes/" src/dashboard/routes.py | head -20
  grep -n "Snapshot\|Change" src/dashboard/context.py
  ```

- [ ] **Step 2: Delete each handler + its template + context builder**

  Edit `src/dashboard/routes.py`: remove route handlers like `snapshot_detail`, `snapshot_content`, `change_detail`, `recent_changes` (if any).

  Edit `src/dashboard/context.py`: remove `get_change_detail_context`, the `Change` summary in the homepage builder, any chunk-loading helpers.

  Delete corresponding templates:
  ```bash
  grep -rn "snapshot_detail\|change_detail" src/dashboard/templates/ | head -10
  git rm src/dashboard/templates/pages/snapshot_detail.html src/dashboard/templates/pages/change_detail.html 2>/dev/null
  ```

- [ ] **Step 3: Remove navigation links and references**

  ```bash
  grep -rn "snapshot\|/changes/" src/dashboard/templates/ 2>&1 | head -10
  ```
  Update sidebar / base template to drop dead links.

- [ ] **Step 4: Run dashboard tests**

  ```bash
  uv run pytest tests/dashboard/ --no-cov 2>&1 | tail -10
  ```
  Delete any test files for the removed pages.

- [ ] **Step 5: Smoke-test the dashboard**

  Start the dev server, navigate to `/`, `/watches`, `/watches/{id}`. Confirm no 500s, no dead links pointing to `/changes/...` or `/snapshots/...`.

- [ ] **Step 6: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop Snapshot/Change dashboard pages + context builders"
  ```

### Task 10.4: Delete `Snapshot` model + migration

**Files:**
- Delete: `src/core/models/snapshot.py`
- Delete: `tests/core/models/test_snapshot.py`
- Modify: `src/core/models/__init__.py`
- Create: `alembic/versions/<rev>_drop_snapshots_table.py`
- Modify: `tests/conftest.py` (remove `make_snapshot` fixture)

**Steps:**

- [ ] **Step 1: Confirm no remaining importers**

  ```bash
  grep -rn "Snapshot\b\|from src.core.models.snapshot" src/ tests/ 2>&1 | grep -v ".pyc"
  ```
  Expected: empty (all consumers gone in Stage 9 + Task 10.3).

- [ ] **Step 2: Delete model + tests + fixture**

  ```bash
  git rm src/core/models/snapshot.py tests/core/models/test_snapshot.py
  ```

  Remove from `src/core/models/__init__.py`. Remove `make_snapshot` from `tests/conftest.py`.

- [ ] **Step 3: Generate drop migration**

  ```bash
  uv run alembic revision --autogenerate -m "drop snapshots + snapshot_chunks tables"
  ```

- [ ] **Step 4: Apply + run full suite**

  ```bash
  uv run alembic upgrade head
  uv run pytest --no-cov 2>&1 | tail -5
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop Snapshot + SnapshotChunk tables"
  ```

### Task 10.5: Delete `src/core/differ.py` + tests

**Files:**
- Delete: `src/core/differ.py`, `tests/core/test_differ.py`
- Delete: `src/core/diff/` directory if it exists

**Steps:**

- [ ] **Step 1: Confirm no remaining importers**

  ```bash
  grep -rn "from src.core.differ\|from src.core.diff\.\|normalize_html\|compute_unified_diff" src/ tests/ 2>&1 | grep -v ".pyc"
  ```

  Known consumer outside `notify.py`: `src/core/notifications/preview_fixtures.py` imports from `src.core.diff`. Either delete `preview_fixtures.py` (if it's a dev-only helper for dropped templates) or stub the diff calls to return `""`. Inspect before deleting.

- [ ] **Step 2: Delete**

  ```bash
  git rm src/core/differ.py tests/core/test_differ.py
  git rm -r src/core/diff/ 2>/dev/null
  ```

- [ ] **Step 3: Run full suite**

- [ ] **Step 4: Commit**

  ```bash
  git add -A
  git commit -m "#156 refactor: drop differ.py + diff/ (chunk-level diff dropped in Phase 5)"
  ```

### Task 10.6: Verify `simhash.py` mirror status; document the keep-decision

**Files:**
- Modify: `src/core/simhash.py` (header comment only)

**Steps:**

- [ ] **Step 1: Check Archiver usage**

  ```bash
  grep -rn "from src.core.simhash\|import simhash" /home/exedev/archiver/src 2>&1 | head -5
  ```

- [ ] **Step 2: Confirm no Watcher usage**

  ```bash
  grep -rn "from src.core.simhash\|simhash" /home/exedev/watcher/src 2>&1 | head -10
  ```
  If Watcher uses simhash anywhere (besides the file itself), remove the call.

- [ ] **Step 3: Either delete or annotate**

  If Archiver doesn't use it either: `git rm src/core/simhash.py tests/core/test_simhash.py`.

  If Archiver uses it: prepend a header comment:
  ```python
  """Simhash — mirrored to /home/exedev/archiver/src/core/simhash.py.

  Phase 5 cutover (#156) removed all Watcher consumers; this file is
  retained solely for mirror parity per AGENTS.md "Mirrored
  content-acquisition code." Delete after Archiver also stops importing.
  """
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add -A
  git commit -m "#156 chore: simhash.py keep-decision (mirror parity)"
  ```

---

## Stage 11 — End-to-end test + CHANGELOG + close

### Task 11.1: End-to-end integration test

**Files:**
- Create: `tests/integration/test_phase5_cutover.py`

**Steps:**

- [ ] **Step 1: Write the test**

  ```python
  # tests/integration/test_phase5_cutover.py
  """E2E: scheduled fetch → scratch → POST → cascade → outbox interlock → drain."""
  import pytest

  pytestmark = pytest.mark.integration


  @pytest.mark.asyncio
  async def test_scheduled_fetch_produces_root_plus_fragments(db_session, ...):
      """One tick produces 1 root + N fragment SourceRevisions; dispatches per Watch."""
      # Arrange: real Archiver dev server on 8021, real InfoSource w/ 2 fragments,
      # 1 root Watch + 2 fragment Watches.
      # Act: invoke check_watch (manually or via Procrastinate test harness).
      # Assert:
      #   - 3 SourceRevisions exist in Archiver for the 3 sources.
      #   - 3 scratch files exist in WATCHER_CACHE_DIR.
      #   - Notifier received 3 send_message calls (one per Watch).
      ...


  @pytest.mark.asyncio
  async def test_outbox_drains_after_archiver_recovery(db_session, ...):
      """Archiver down → enqueue → recovery → drain → POST → dispatch."""
      ...
  ```

  Use the existing dev-server-on-8021 setup; mock the Notifier SDK.

- [ ] **Step 2: Run**

  ```bash
  uv run pytest tests/integration/test_phase5_cutover.py -m integration --no-cov 2>&1 | tail -10
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add -A
  git commit -m "#156 test: end-to-end Phase 5 cutover integration test"
  ```

### Task 11.2: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

**Steps:**

- [ ] **Step 1: Add the entry**

  Append at the top of `CHANGELOG.md`:
  ```markdown
  ## v2.0.0 (2026-MM-DD) — Phase 5 cutover

  **Breaking** — Watcher refactored to produce SourceRevisions in Archiver.

  - Watch table reshaped: `info_item_id` → `info_source_id`.
  - Content persistence dropped: no more `Snapshot`/`Change`/`differ.py`/chunk diffs.
  - Notification trigger moved from `info.changes` consumption to inline POST-success sites; outbox drain re-fires deferred notifications.
  - New `pending_source_revisions` outbox guarantees delivery to Archiver.
  - Scratch cache at `WATCHER_CACHE_DIR` with sweeper + outbox interlock.
  - SDK pin: `archiver-client>=2.2.0,<3`.

  See [docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md).
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add CHANGELOG.md
  git commit -m "#156 docs: CHANGELOG entry for Phase 5 cutover"
  ```

### Task 11.3: Close GH #156

**Files:** None.

**Steps:**

- [ ] **Step 1: Comment + close**

  ```bash
  export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
  gh issue comment 156 --body "Phase 5 implementation complete. See docs/plans/2026-05-13-phase-5-watcher-v2-cutover-plan.md."
  gh issue close 156
  ```

---

## Wrap-up

After Task 11.3:

- `watches.info_source_id` is the binding column; `info_item_id` is gone.
- Watcher produces SourceRevisions in Archiver via client-allocated ULIDs; scratch cache is write-before-POST.
- `pending_source_revisions` outbox + drain worker handles Archiver downtime; outbox-drain success dispatches deferred notifications.
- Sweeper cleans scratch files except those reserved by un-drained outbox rows.
- Notification dispatch fires from POST-success sites via existing `dispatch_event_notifications`; `notify.py` no longer loads Snapshot bytes for diffs.
- `Snapshot`, `Change`, `differ.py`, `info.changes` producer plumbing, `/api/v1/changes` routes, and dashboard Snapshot/Change pages are all removed.

### Operational follow-ups (out of scope)

- #157 — redirect conveyance workflow (Watcher → Archiver).
- `simhash.py` mirror discipline — revisit when Replicator joins the consumer set (Phase 6).
- If chunk-level diffs become a re-stated requirement, design a new approach (Archiver bytes cache lookup, or a Watcher-local "previous extracted text" sidecar keyed by `info_source_id`).

---

## Skills referenced

- @superpowers:test-driven-development — every code-changing task starts with a failing test.
- @superpowers:verification-before-completion — run `uv run pytest --no-cov` + `uv run ruff check .` before claiming a task done.
- @superpowers:subagent-driven-development — recommended for execution (fresh subagent per task + review between tasks).
- @superpowers:executing-plans — alternative for inline execution in a single session.
- @superpowers:using-git-worktrees — if the worktree pre-flight option is chosen.
