# Watcher Phase 2c — Hard Cutover to InfoItem-Native Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Watcher from a URL-native service into an InfoItem-native consumer of the Information service: every Watch references an `info_item_id`, the canonical URL + fetch/extract/fingerprint config lives in the primary InfoSpec, and the change bus emits envelopes keyed by `info_item_id`.

**Architecture:** Hard cutover in a single PR. Watch's `url` and `fetch_config` columns are dropped; Watch gains a required `info_item_id` FK to `information.info_items`. The check pipeline resolves the primary InfoSpec via the SDK's TTL cache at fetch time, with `force_refresh` retry on extraction failure. The drain envelope (`schema_version: 2`) carries `info_item_id`, `info_spec_id`, and pre/post fingerprint values; partition key is `info_item_id`. `drain_changes_outbox` runs every minute via Procrastinate's `@bp.periodic` decorator (cron's minimum cadence; sub-minute is not expressible in 5-field cron and is a follow-up if needed).

**Tech Stack:** SQLAlchemy 2 async, Alembic, Postgres (cross-schema FK from `public.watches` → `information.info_items`), Procrastinate (periodic tasks), `information-client` SDK, Redis Streams, FastAPI, Jinja2 + HTMX dashboard.

**Design doc:** `docs/plans/2026-05-03-information-source-specifications-design.md`
**Issue:** #138

---

## File Structure

### New files
- `alembic/versions/<rev>_phase2c_watch_info_item_cutover.py` — multi-step migration: add `watches.info_item_id`, backfill from existing watches, drop `url` + `fetch_config`, alter NOT NULL.
- `src/core/registry/information.py` — lazy `InformationClient` factory + accessor on the existing `ServiceRegistry`. (Or extend the existing registry module — TBD per the codebase's registry layout; create a new file only if there isn't a clean extension point.)
- `src/core/info_resolver.py` — small helper that hides the SDK call + `force_refresh` retry pattern from the pipeline. Returns `ResolvedInfoSpec(info_item_id, info_spec_id, document)`.
- `src/dashboard/templates/partials/info_item_picker.html` — Jinja partial for an InfoItem dropdown on the Watch create form.
- `tests/core/test_info_resolver.py` — unit tests for the resolver helper (cache hit/miss, force_refresh retry).

### Modified files
- `alembic/env.py` — add `include_object` filter to limit autogenerate to the `public` schema; set `include_schemas=True` so cross-schema FKs render correctly.
- `src/core/models/watch.py` — drop `url`, `fetch_config`; add `info_item_id` (required, FK).
- `src/workers/tasks.py` — `check_watch` resolves URL + fetch defaults from primary InfoSpec; force-refresh on extraction failure; thread `info_item_id` + `info_spec_id` + previous/current fingerprint values to the Change row. Also fix the three event-emission sites (lines 121, 162, 175 — `WATCH_ERROR`, `WATCH_RECOVERED`, `CHANGE_DETECTED`) that read `watch.url`.
- `src/workers/pipeline.py` — `_run_check_pipeline` accepts a resolved spec object instead of reading `watch.url` / `watch.fetch_config`. Selectors come from `extraction.selector` (single value, was a multi-element list). Also line 343 (`capture_screenshot(watch.url, ...)`) needs the resolved URL.
- `src/workers/changes_drain.py` — `_build_envelope` bumps to `schema_version: 2`, adds `info_item_id`, `info_spec_id`, `previous_fingerprint`, `current_fingerprint`; partition key becomes `info_item_id`. Drain wraps body in `pg_try_advisory_xact_lock(<lock_id>)` so manual + cron drains can't double-publish.
- `src/workers/changes_drain.py` (decorator) — stack `@bp.periodic(cron="* * * * *")` above the existing `@bp.task(...)` so the task fires every minute. (Procrastinate periodic uses 5-field cron via croniter — sub-minute cadence is not expressible.) No edit to `src/workers/__init__.py` needed; `changes_drain` is already imported there.
- `src/api/routes/v1.py` (or wherever Watch CRUD lives) — Watch create/update endpoints accept `info_item_id` instead of `url` + `fetch_config`.
- `src/api/schemas/watch.py` — Pydantic schemas reflect the new contract.
- `src/dashboard/routes.py` — Watch create/edit handlers swap URL field for InfoItem picker; detail page shows resolved URL via the SDK; `WATCH_FIELD_META` is rebuilt to drop every `fetch_config`-source row (timeout, headers, ignore_patterns, selectors, exclude_selectors, dynamic_id_patterns, strip_boilerplate, skip_empty_pages, file_format, chunk_row_size, sort_columns, sheet_name, viewport_width, viewport_height) and every `url`-source row. Inline-edit POST endpoints for those fields are removed; their templates (`partials/watch_field*.html`) lose their references in lockstep.
- `src/dashboard/templates/pages/watch_detail.html` — read URL from a `resolved_url` template variable rather than `watch.url`.
- `src/dashboard/templates/pages/watch_form.html` (single create + edit template — there is no `watch_create.html`) — InfoItem picker partial replaces the URL/fetch_config form fields.
- `src/dashboard/templates/partials/notification_variable_chips.html` — `watch_url` template variable still resolves; documentation update only.
- `src/core/notifications/content.py` and `src/workers/tasks.py` (event-emission sites) — pass resolved URL instead of `watch.url`.
- `src/core/watches.py` — same; `resolve_watch_url(watch, client)` helper for any remaining callsites.
- `src/core/models/change.py` — add `info_item_id`, `info_spec_id`, `previous_fingerprint`, `current_fingerprint` columns. Migrated alongside the Watch migration.
- `src/api/main.py` (lifespan) — construct + close `InformationClient` once per process. Closes via `await reg.get_information_client().aclose()` at shutdown.
- `tests/conftest.py` — fixture overhauls: factory functions for `make_info_item()`, `make_info_spec()`; `make_watch()` requires `info_item_id` and auto-creates a default InfoItem + primary InfoSpec when none is provided. Migrated **before** the Watch model loses its columns (Task 0) so subsequent tasks have a working test substrate.
- `AGENTS.md` — documentation sweep: update Watch description, env-var list, dev commands.

### Deleted files
- None expected.

### Out of scope (explicit)
- Inline InfoItem creation from the Watch form (operator must create the InfoItem in the Information service first; v1 picker just selects from existing).
- PDF/file content-type support: the v1 InfoSpec schema only enumerates `css | xpath | jsonpath | regex | full_page`. The cutover migration aborts if any non-HTML watch exists. (Current prod has 3 HTML watches — verified.)
- Multi-selector InfoSpec extension: `extraction.selector` is single-valued in v1. Existing watches all use ≤1 selector; the migration comma-joins if it ever encounters multi-element lists. CSS comma-list semantics differ from independent-list extraction (UNION vs concat) — verified zero multi-selector watches in current prod, but this is a real semantic difference and a follow-up if multi-selector returns.
- Sub-minute drain cadence (cron's minimum is 1 minute). If 30 s is required later, replace `@bp.periodic` with a dedicated asyncio loop or a separate worker task.
- `fetch_config` knobs that the v1 InfoSpec schema cannot represent (`headers`, `ignore_patterns`, `exclude_selectors`, `ignore_selectors`, `dynamic_id_patterns`, `strip_boilerplate`, `skip_empty_pages`, `file_format`, `chunk_row_size`, `sort_columns`, `sheet_name`, `viewport_width`, `viewport_height`). The cutover migration **aborts if any of these are populated on existing watches** — verified zero usage on current 3 watches before writing this plan. If any appear later, the plan needs an InfoSpec schema extension first.
- Notifier extraction work (`docs/plans/2026-05-02-notifier-adapter.md`).

---

## Cross-Cutting Decisions

**Cross-schema FK.** `watches.info_item_id` references `information.info_items.info_item_id` with `ON DELETE RESTRICT`. Same Postgres database, two schemas — Postgres supports this natively. Watcher's alembic `env.py` does **not** currently filter; without the filter, autogenerate would either spuriously emit `drop_table` ops for `information.*` (they exist live but aren't in `target_metadata`) or render foreign keys to those tables incorrectly. **Task 0** adds an `include_object` filter scoped to `schema is None` (i.e. `public`) plus `include_schemas=True` so cross-schema FKs render. Watcher's migration manages only its own column; it does **not** create or alter the `information.*` tables.

**Migration order (single revision).** To avoid stranding the schema in a half-migrated state:
1. Add `watches.info_item_id` (nullable, no FK yet).
2. Inline data migration: for each existing Watch, insert an `information.info_items` row + an `information.info_specs` row built from `(name, url, fetch_config)`, then UPDATE `watches.info_item_id`.
3. Add the FK constraint (`ON DELETE RESTRICT`).
4. ALTER `watches.info_item_id` SET NOT NULL.
5. DROP `watches.url`, `watches.fetch_config`.

The downgrade restores `url` + `fetch_config` from the primary InfoSpec, then drops `info_item_id`. Test data only — production downgrade is a one-shot rescue, not routine.

**SDK lifecycle.** The current `ServiceRegistry` (verified: `src/core/registry.py`, `get_registry()` singleton at line 47) only holds `_fetcher` + `_extractor_map`. We extend it with a lazy `get_information_client()` accessor that reads `INFORMATION_BASE_URL` + `INFORMATION_API_KEY` from env on first call and caches the client. Lifespan startup pre-warms the client; lifespan shutdown calls `await reg.get_information_client().aclose()`. Procrastinate workers run in the same process today (in-process worker via `start_application_worker`), so they share the same client instance via the registry. If we ever split workers off, each process gets its own TTL cache — fine.

**Cache + force-refresh.** Pipeline calls `client.get_primary_info_spec(info_item_id)`; on extraction failure (selector no longer matches, etc.), it calls again with `force_refresh=True` and retries the extraction once. The SDK's `force_refresh=True` re-fetches and overwrites the cache entry (verified at `clients/python/src/information_client/client.py:148`); it does **not** call `invalidate_primary_cache`. Tests must assert the `force_refresh=True` call, not cache invalidation. If the second attempt also fails, the change becomes a `WATCH_ERROR` event as today.

**Information service unreachability.** Distinct from the missing-InfoItem case (`NotFound`):
- `NotFound` (404): the watch's `info_item_id` references a deleted InfoItem. Log + return `{"skipped": True, "reason": "info_item_missing"}`. Cron will retry on next tick; operator action is to delete the orphaned watch or re-link.
- `ConnectionError` / `TimeoutError`: the Information service is down. **Re-raise** so Procrastinate's existing `RetryStrategy(retry_exceptions={ConnectionError, TimeoutError})` retries the task with exponential backoff (max_attempts=3 today). After 3 attempts, the job is dead-lettered and the watch is skipped until the next scheduled check. We do not emit `WATCH_ERROR` here — that's reserved for fetch-target errors, not infrastructure.
- `ServerError` (5xx): same as ConnectionError — re-raise, retry. The Information service should not return 5xx for valid requests.
- `AuthError`: re-raise loud. Operator must fix `INFORMATION_API_KEY`.

**Fingerprints on Change rows.** The current `Change` model stores `previous_snapshot_id` + `current_snapshot_id` and `change_metadata` (JSONB). The drain envelope needs raw fingerprint values, not just snapshot IDs. We add two columns: `previous_fingerprint` (nullable) + `current_fingerprint` (nullable, but always set for non-initial changes). These match `fingerprint.algorithm` from the InfoSpec — for v1, simhash-as-int64 already exists on snapshots; we copy it onto the Change at insertion time so the drain doesn't need a snapshot join.

**Partition key.** `info_item_id` (string ULID). Same InfoItem's stream of changes always lands in the same Redis Stream consumer-group partition slot; downstream Archive can preserve ordering per InfoItem.

**Periodic drain.** Procrastinate's `@bp.periodic(cron="* * * * *")` decorator stacks above the existing `@bp.task(...)` on `drain_changes_outbox` (mirrors the `schedule_tick` pattern at `src/workers/tasks.py:188`). Cadence is once per minute — cron's minimum.

**Concurrency safety on drain.** A manual drain (e.g. via the test suite or an operator command) racing the cron drain would both call `select_unpublished` and double-publish to Redis Streams. Two mitigations, both applied:
1. **Advisory lock at drain start.** Wrap the body in `pg_try_advisory_xact_lock(<lock_id>)`; if the lock fails, log and return `{"skipped": True, "reason": "drain_already_running"}`. Lock ID is a constant chosen for this task.
2. **Consumer-side dedup is still required.** The envelope includes `change_id`; downstream consumers (Archive in Phase 3+) must dedupe by it because partition reassignment + replay can also generate near-duplicates. The reference consumer is fine without this — it's exactly-once at the file-write level since acks happen post-write.

---

## Task Breakdown

Estimated 13 tasks (Tasks 0–12). Each ends with a green test and a commit.

### Task 0: Pre-flight — alembic schema filter + test fixture migration

**Why first.** Three foundational changes that every later task depends on:
1. `alembic/env.py` currently filters nothing. Without an `include_object` filter and `include_schemas=True`, autogenerate at Task 3 will misbehave on the cross-schema `information.info_items` reference.
2. The test engine fixture (`tests/conftest.py:55-58`) calls `Base.metadata.create_all` on **Watcher's** `Base` only. The Information service has its own separate `DeclarativeBase` (`src/information/core/models/base.py:32`); its tables (`information.info_items`, `information.info_specs`) don't exist on the test DB until we explicitly create them.
3. Watch construction in tests is widespread (15 test files contain `Watch(...)` invocations; ~174 construction sites). Many use `fetch_config={"selectors": [...], "ignore_patterns": [...], ...}` that flows through `**kwargs` and silently breaks post-Task 4. The test fixture migration must land **before** the model changes, or every later task breaks the suite irrecoverably.

**Files:**
- Modify: `alembic/env.py`
- Modify: `tests/conftest.py` (extend `test_engine` fixture; rewrite the existing `make_watch` and `make_snapshot` fixtures rather than create new ones — current fixtures live at `tests/conftest.py:109-130`)
- Modify: every test file that constructs `Watch(...)` directly (15 files; see Step 7).

- [ ] **Step 1: Create the feature worktree**

```bash
cd /home/exedev/watcher
git worktree add .worktrees/feat-138-phase2c -b feat/138-phase2c-cutover main
cd .worktrees/feat-138-phase2c
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
```

- [ ] **Step 2: Confirm baseline tests pass + Information migrations at head**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3   # all green; record count for diff awareness
uv run alembic -c alembic_information.ini current
uv run alembic -c alembic_information.ini upgrade head
uv run alembic current
```

- [ ] **Step 3: Add `include_object` + `include_schemas` to `alembic/env.py`**

```python
# alembic/env.py — at module scope
def _include_object(object, name, type_, reflected, compare_to):
    """Restrict autogenerate to the public schema (Watcher's tables)."""
    if hasattr(object, "schema") and object.schema not in (None, "public"):
        return False
    return True

# In both run_migrations_offline and run_migrations_online configure() calls:
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_item=render_item,
    include_object=_include_object,
    include_schemas=True,
)
```

- [ ] **Step 4: Verify autogenerate is now clean**

```bash
uv run alembic revision --autogenerate -m "noop check" --rev-id phase2c_noop_check
```

Inspect the generated file. Expected: empty `upgrade()` / `downgrade()` (no spurious drops of `information.*` tables).

```bash
rm alembic/versions/phase2c_noop_check_*.py
```

- [ ] **Step 5: Audit existing watches for fetch_config keys we cannot migrate**

```bash
uv run python3 -c "
import os, asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

UNSUPPORTED = {'headers', 'ignore_patterns', 'exclude_selectors', 'ignore_selectors',
               'dynamic_id_patterns', 'strip_boilerplate', 'skip_empty_pages',
               'file_format', 'chunk_row_size', 'sort_columns', 'sheet_name',
               'viewport_width', 'viewport_height'}
# Note: 'timeout' is intentionally NOT in UNSUPPORTED — InfoSpec v1 has
# target.fetch.timeout_seconds. The Task 4 backfill maps fetch_config.timeout
# to that field.

async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as c:
        rows = await c.execute(text('SELECT id, name, fetch_config FROM watches'))
        bad = []
        for r in rows:
            keys = set((r.fetch_config or {}).keys())
            if keys & UNSUPPORTED:
                bad.append((str(r.id), r.name, sorted(keys & UNSUPPORTED)))
        if bad:
            print('BLOCKED — manual cleanup needed:')
            for b in bad: print(f'  {b[0]} ({b[1]!r}) — keys: {b[2]}')
        else:
            print('OK — no unsupported fetch_config keys in use')
    await engine.dispose()

asyncio.run(main())
"
```

If output starts with `BLOCKED`, stop and resolve manually before continuing. Expected on current DB: `OK`.

- [ ] **Step 6a: Extend `test_engine` to create the Information schema + tables**

The existing test_engine fixture (`tests/conftest.py:43-83`) calls `Base.metadata.create_all` on Watcher's `Base` only. Add explicit Information schema creation:

```python
# tests/conftest.py — inside test_engine fixture, before the existing create_all
from src.information.core.models.base import Base as InformationBase
from src.information.core.models.info_item import InfoItem  # noqa: F401 — register
from src.information.core.models.info_spec import InfoSpec  # noqa: F401 — register
from sqlalchemy import text

async with engine.begin() as conn:
    await conn.execute(text("CREATE SCHEMA IF NOT EXISTS information"))
    await conn.run_sync(InformationBase.metadata.create_all)
    await conn.run_sync(Base.metadata.create_all)  # existing
    # ...existing trigger recreation...
```

And the matching teardown:

```python
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.drop_all)
    await conn.run_sync(InformationBase.metadata.drop_all)
    await conn.execute(text("DROP SCHEMA IF EXISTS information CASCADE"))
```

- [ ] **Step 6b: Rewrite the existing `make_watch` and `make_snapshot` fixtures**

The existing fixtures are pytest fixtures (`@pytest.fixture` decorated) at `tests/conftest.py:109-130`. The plan needs **callable factories**, not fixtures, so tests can build multiple Watches per test. Convert them to module-level async functions (or use `pytest_factoryboy`). Remove the `@pytest.fixture` decorators; rename the fixture forms (if any tests rely on them) to `default_watch_fixture` / `default_snapshot_fixture` so callers don't collide.

```python
# tests/conftest.py — module-level (NOT @pytest.fixture)
async def make_info_item(session, *, name="Test Item", description=None):
    from src.information.core.models.info_item import InfoItem
    item = InfoItem(name=name, description=description)
    session.add(item)
    await session.flush()
    return item


async def make_info_spec(
    session,
    info_item,
    *,
    url="https://example.com",
    selector=None,
    fingerprint_algorithm="simhash",
    priority=1,
    active=True,
):
    """Default priority=1 active=True. Tests creating multiple specs per item must
    pass distinct priorities to avoid violating uq_info_specs_active_priority_per_item.
    """
    from src.information.core.models.info_spec import InfoSpec
    extraction = (
        {"algorithm": "css", "selector": selector}
        if selector
        else {"algorithm": "full_page"}
    )
    document = {
        "schema_version": 1,
        "target": {"url": url},
        "extraction": extraction,
        "fingerprint": {"algorithm": fingerprint_algorithm},
    }
    spec = InfoSpec(
        info_item_id=info_item.info_item_id,
        schema_version=1,
        document=document,
        priority=priority,
        active=active,
    )
    session.add(spec)
    await session.flush()
    return spec


async def make_watch(
    session,
    *,
    name="Test Watch",
    info_item_id=None,
    content_type=None,
    url=None,
    selector=None,
    **kwargs,
):
    """Construct a Watch with an auto-created InfoItem + primary InfoSpec.

    Phase 2c migration shim — handles all four cutover states:
      Task 0 ships:        Watch has url, NO info_item_id.
      Task 3 ships:        Watch has url AND info_item_id (nullable).
      Task 4 ships:        Watch has info_item_id (NOT NULL), NO url, NO fetch_config.

    The helper detects column presence at runtime via hasattr. Callers should pass
    url=/selector= as if specifying the InfoSpec; the helper threads them through
    appropriately for whichever migration state the model is currently in.

    Tests using `fetch_config=` in **kwargs will silently fail post-Task 4 — the
    Step 7 mechanical migration must remove them.
    """
    from src.core.models.watch import ContentType, Watch

    if info_item_id is None:
        info_item = await make_info_item(session, name=name)
        await make_info_spec(
            session, info_item, url=url or "https://example.com", selector=selector
        )
        info_item_id = info_item.info_item_id

    watch_kwargs = {"name": name, **kwargs}
    watch_kwargs.setdefault("content_type", content_type or ContentType.HTML)

    # Task 3+ — set info_item_id only if the column exists
    if hasattr(Watch, "info_item_id"):
        watch_kwargs["info_item_id"] = info_item_id

    # Pre-Task-4 — Watch.url is NOT NULL; always inject a value
    if hasattr(Watch, "url") and "url" not in watch_kwargs:
        watch_kwargs["url"] = url or "https://example.com"

    # Pre-Task-4 — Watch.fetch_config has a server default; only inject if caller wants
    # a specific value. (Don't inject for selector — that goes on the InfoSpec only.)

    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()
    return watch
```

**Why both `hasattr` guards.** The plan ships in this exact order: Task 0 ships first (Watch has only `url`), Task 3 ships next (Watch has both `url` AND `info_item_id`), Task 4 ships last (Watch has only `info_item_id`). The same fixture must work in all three states.

- [ ] **Step 7: Migrate every test file that constructs `Watch(...)` directly**

```bash
grep -rln "Watch(" /home/exedev/watcher/tests/   # 15 files, ~174 construction sites
grep -rln "fetch_config=" /home/exedev/watcher/tests/  # locate fetch_config kwargs to translate
```

**Mechanical translation rules:**
- `Watch(name="X", url="Y", content_type=Z)` → `await make_watch(session, name="X", url="Y", content_type=Z)`.
- `Watch(name="X", url="Y", fetch_config={"selectors": ["S"]})` → `await make_watch(session, name="X", url="Y", selector="S")` (selectors → InfoSpec via the helper). If the test had multiple selectors, comma-join them: `selector="A, B"`.
- `Watch(name="X", url="Y", fetch_config={"ignore_patterns": [...]})` etc. → **drop the fetch_config kwarg entirely**. These knobs aren't expressible in InfoSpec v1; tests that exercised that behaviour need rewriting in Task 7 (worker logic) or are dead post-cutover.
- Bare `Watch()` placeholders → `await make_watch(session)`.

**Per-file commit groups (15 files split for reviewability):**
- Group A (model + core): `tests/core/models/test_watch.py`, `tests/core/test_watches.py`, `tests/core/test_probe.py`, `tests/core/test_models.py`
- Group B (notifications): `tests/core/notifications/test_notify_remote.py`, `tests/core/notifications/test_content.py`, `tests/workers/test_notify.py`
- Group C (workers + dashboard): `tests/workers/test_pipeline.py`, `tests/workers/test_tasks.py`, `tests/dashboard/*`, `tests/api/*`

For each group: migrate, run `uv run pytest tests/<group> --no-cov -m "not integration"`, confirm green, commit. Three commits within Task 0.

- [ ] **Step 8: Run the full unit suite, confirm green**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3
```

- [ ] **Step 9: Commit**

```bash
git add alembic/env.py tests/conftest.py tests/factories/ tests/
git commit -m "#138 chore: alembic schema filter + make_watch fixture for Phase 2c"
```

---

### Task 1: Confirm Information service is reachable + branch state

(Brief environment check — kept as a small standalone task so subagents have a clean rendezvous point after the voluminous Task 0.)

- [ ] **Step 1: Confirm Task 0 changes are committed and on the right branch**

```bash
git log --oneline -5
git status
```

- [ ] **Step 2: Confirm Information service is reachable**

```bash
curl -s http://localhost:8020/healthz || sudo systemctl status information
```

---

### Task 2: Add `Change` columns for fingerprints + InfoSpec linkage

**Why first.** The drain envelope refinement (Task 9) needs these columns. Adding them up front means later tasks can populate them without revisiting the schema. This is also the simplest migration to get right.

**Files:**
- Create: `alembic/versions/<rev>_add_change_info_columns.py`
- Modify: `src/core/models/change.py`
- Test: `tests/core/models/test_change.py`

- [ ] **Step 1: Write a failing test for the new fields**

`tests/core/models/test_change.py` — add a test that constructs a `Change` with `info_item_id`, `info_spec_id`, `previous_fingerprint`, `current_fingerprint` and round-trips it.

- [ ] **Step 2: Run the test, confirm it fails**

```bash
uv run pytest tests/core/models/test_change.py -k "info" --no-cov 2>&1 | tail -5
```

Expected: failure on the new attributes (they don't exist yet).

- [ ] **Step 3: Add the columns to the model**

```python
# src/core/models/change.py — add to the Change class
info_item_id: Mapped[ULID | None] = mapped_column(
    ULIDType, nullable=True, index=True, default=None,
)
info_spec_id: Mapped[ULID | None] = mapped_column(
    ULIDType, nullable=True, default=None,
)
previous_fingerprint: Mapped[int | None] = mapped_column(
    BigInteger, nullable=True, default=None,
)
current_fingerprint: Mapped[int | None] = mapped_column(
    BigInteger, nullable=True, default=None,
)
```

The columns are nullable for now; Task 4 makes `info_item_id` mandatory once the data migration runs. `previous_fingerprint` is permanently nullable (initial change for an InfoItem has none).

- [ ] **Step 4: Generate the alembic revision**

```bash
uv run alembic revision --autogenerate -m "add change info_item_id, info_spec_id, fingerprint columns"
```

Inspect the generated file; confirm only `changes`-table additions, no spurious drops. Strip Procrastinate-table noise if alembic emits any (recurring gotcha — see prior plans).

- [ ] **Step 5: Add an index on `(info_item_id, detected_at DESC)` for downstream queries**

Use SQLAlchemy column expressions for the DESC ordering — string + `sa.text` mixing is brittle:

```python
op.create_index(
    "ix_changes_info_item_id_detected_at",
    "changes",
    [sa.column("info_item_id"), sa.column("detected_at").desc()],
)
```

Drop in downgrade.

- [ ] **Step 6: Apply + run the test**

```bash
uv run alembic upgrade head
uv run pytest tests/core/models/test_change.py --no-cov 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 7: Recreate the trigger on the test engine**

Per AGENTS.md DB Triggers gotcha — confirm `tests/conftest.py` `test_engine` fixture's `create_all` still wires `trg_changes_update_last_changed_at`. No new triggers in this task, just verifying the existing one still applies.

- [ ] **Step 8: Commit**

```bash
git add src/core/models/change.py alembic/versions/<rev>_*.py tests/core/models/test_change.py
git commit -m "#138 feat: add info_item_id, info_spec_id, fingerprint columns to changes"
```

---

### Task 3: Watch model — add `info_item_id` (nullable, no data move yet)

**Why before the data migration.** Lets us update tests + the worker pipeline behind the new shape before we drop the old columns. The migration in this task adds only the column + FK; data backfill + drop happens in Task 4.

**Files:**
- Create: `alembic/versions/<rev>_add_watch_info_item_id.py`
- Modify: `src/core/models/watch.py`
- Test: `tests/core/models/test_watch.py`

- [ ] **Step 1: Write a failing test that constructs a Watch with info_item_id**

```python
# tests/core/models/test_watch.py
from src.information.core.models.info_item import InfoItem  # exact path; not re-exported via __init__.py — verify before write

async def test_watch_accepts_info_item_id(db_session):
    info_item = InfoItem(name="Test")
    db_session.add(info_item)
    await db_session.flush()
    w = Watch(
        name="Test",
        url="https://example.com",  # still required this task
        content_type=ContentType.HTML,
        info_item_id=info_item.info_item_id,
    )
    db_session.add(w)
    await db_session.flush()
    assert w.info_item_id == info_item.info_item_id
```

(Both schemas live in the same DB, so the test session can write to `information.*` directly.)

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/core/models/test_watch.py::test_watch_accepts_info_item_id --no-cov 2>&1 | tail -10
```

Expected: AttributeError on `info_item_id`.

- [ ] **Step 3: Add column to Watch**

```python
# src/core/models/watch.py
from sqlalchemy import ForeignKey
# ...
info_item_id: Mapped[ULID | None] = mapped_column(
    ULIDType,
    ForeignKey("information.info_items.info_item_id", ondelete="RESTRICT"),
    nullable=True,
    index=True,
    default=None,
)
```

- [ ] **Step 4: Generate the migration**

```bash
uv run alembic revision --autogenerate -m "add watches.info_item_id (nullable)"
```

Verify the migration creates only the column + index + FK; strip noise.

- [ ] **Step 5: Apply + run the test**

```bash
uv run alembic upgrade head
uv run pytest tests/core/models/test_watch.py::test_watch_accepts_info_item_id --no-cov 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/core/models/watch.py alembic/versions/<rev>_*.py tests/core/models/test_watch.py
git commit -m "#138 feat: add nullable watches.info_item_id with cross-schema FK"
```

---

### Task 4: Data migration — backfill InfoItem + InfoSpec for existing watches; drop `url` + `fetch_config`; mark `info_item_id` NOT NULL

**Why a single migration revision.** The backfill must be atomic with the drop: rollback safety. If we split, a half-migrated DB has watches without info_item_id and without url.

**Files:**
- Create: `alembic/versions/<rev>_phase2c_watch_cutover.py`

- [ ] **Step 1: Pre-flight check — abort on non-HTML or unsupported `fetch_config` keys**

The migration's `upgrade()` runs **two** guards before any backfill. Both are fail-fast — abort with a useful error.

```python
# Guard 1: non-HTML watches (InfoSpec v1 schema only enumerates HTML extraction algorithms)
non_html = conn.execute(sa.text(
    "SELECT id, name, content_type FROM watches WHERE content_type != 'html'"
)).fetchall()
if non_html:
    raise RuntimeError(
        f"Phase 2c migration aborted: {len(non_html)} non-HTML watches present. "
        f"InfoSpec v1 only supports HTML extraction. Manually re-key or delete: "
        f"{[(r.id, r.name) for r in non_html]}"
    )

# Guard 2: unsupported fetch_config keys (InfoSpec v1 cannot represent them)
UNSUPPORTED = {
    "headers", "ignore_patterns", "exclude_selectors", "ignore_selectors",
    "dynamic_id_patterns", "strip_boilerplate", "skip_empty_pages",
    "file_format", "chunk_row_size", "sort_columns", "sheet_name",
    "viewport_width", "viewport_height", "timeout",
}
rows = conn.execute(sa.text("SELECT id, name, fetch_config FROM watches")).fetchall()
bad = []
for r in rows:
    keys = set((r.fetch_config or {}).keys())
    extras = keys & UNSUPPORTED
    if extras:
        bad.append((r.id, r.name, sorted(extras)))
if bad:
    raise RuntimeError(
        "Phase 2c migration aborted: watches use fetch_config keys that the "
        "v1 InfoSpec schema cannot represent. Either delete the keys, extend "
        f"the InfoSpec schema first, or remove the watches: {bad}"
    )
```

The pre-flight script in Task 0 Step 5 should have already surfaced any blockers. This guard is in the migration itself as a belt-and-suspenders check in case the operator runs against a different DB.

- [ ] **Step 2: Backfill InfoItem + InfoSpec rows from existing watches**

```python
import json
from ulid import ULID

watches = conn.execute(sa.text(
    "SELECT id, name, url, fetch_config, content_type FROM watches "
    "WHERE info_item_id IS NULL"
)).fetchall()

for w in watches:
    info_item_id = ULID()
    info_spec_id = ULID()
    fc = w.fetch_config or {}
    selectors = fc.get("selectors") or []
    if selectors:
        algorithm = "css"
        selector = ", ".join(selectors)
    else:
        algorithm = "full_page"
        selector = None

    target = {"url": w.url}
    if "timeout" in fc:
        target["fetch"] = {"timeout_seconds": int(fc["timeout"])}

    document = {
        "schema_version": 1,
        "target": target,
        "extraction": (
            {"algorithm": algorithm}
            if algorithm == "full_page"
            else {"algorithm": algorithm, "selector": selector}
        ),
        "fingerprint": {"algorithm": "simhash"},
    }

    conn.execute(
        sa.text(
            "INSERT INTO information.info_items "
            "(info_item_id, name, description, owner, created_at, updated_at) "
            "VALUES (:id, :name, NULL, NULL, now(), now())"
        ),
        {"id": str(info_item_id), "name": w.name},
    )
    conn.execute(
        sa.text(
            "INSERT INTO information.info_specs "
            "(info_spec_id, info_item_id, schema_version, document, "
            "priority, active, created_at) "
            "VALUES (:sid, :iid, 1, CAST(:doc AS jsonb), 1, TRUE, now())"
        ),
        {"sid": str(info_spec_id), "iid": str(info_item_id), "doc": json.dumps(document)},
    )
    conn.execute(
        sa.text("UPDATE watches SET info_item_id = :iid WHERE id = :wid"),
        {"iid": str(info_item_id), "wid": str(w.id)},
    )
```

- [ ] **Step 3: Tighten constraints, drop columns**

```python
op.alter_column("watches", "info_item_id", nullable=False)
op.drop_column("watches", "url")
op.drop_column("watches", "fetch_config")
```

- [ ] **Step 4: Implement the downgrade**

The downgrade re-adds the columns nullable, copies `target.url` from the primary InfoSpec back into `watches.url`, and re-builds `fetch_config.selectors` from `extraction.selector`. Document that downgrade does NOT delete the InfoItem/InfoSpec rows — operator decision.

- [ ] **Step 5: Run the migration on a fresh test DB**

```bash
# Recreate test DB to verify clean apply
psql -d watcher_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; DROP SCHEMA information CASCADE; CREATE SCHEMA information;" || true
uv run alembic -c alembic_information.ini upgrade head
uv run alembic upgrade head
```

Expected: clean apply, no errors.

- [ ] **Step 6: Run on the real dev DB (3 watches)**

```bash
uv run alembic upgrade head
# Verify
uv run python3 -c "
import os, asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as c:
        r = await c.execute(text('SELECT id, name, info_item_id FROM watches'))
        for row in r: print(row)
        r = await c.execute(text('SELECT info_item_id, name FROM information.info_items'))
        for row in r: print(row)
    await engine.dispose()
asyncio.run(main())
"
```

Expected: 3 watches each linked to a fresh InfoItem.

- [ ] **Step 7: Update Watch model — drop `url` + `fetch_config`, mark info_item_id required**

```python
# src/core/models/watch.py — remove url, fetch_config, validates(content_type)... (keep)
# info_item_id becomes nullable=False
```

- [ ] **Step 8: Run the full unit suite**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3
```

Expected: green. The Task 0 `make_watch` helper already auto-creates InfoItem + InfoSpec; with the model column gone, the `hasattr(Watch, "url")` branch in `make_watch` becomes dead and can be tightened.

- [ ] **Step 9: Tighten `make_watch` (remove the `hasattr(Watch, "url")` shim)**

Now that `Watch` no longer has `url` or `fetch_config`, drop the dual-mode branches in `tests/conftest.py`:

```python
# Drop these lines now that the column is gone:
if url is not None and "url" not in kwargs and hasattr(Watch, "url"):
    watch_kwargs["url"] = url
```

The helper still accepts `url=` and `selector=` kwargs for the InfoSpec it auto-creates — those are the only places they go now.

- [ ] **Step 10: Commit**

```bash
git add alembic/versions/<rev>_*.py src/core/models/watch.py \
    tests/core/models/test_watch.py tests/conftest.py
git commit -m "#138 feat: cutover watches.url+fetch_config to info_item_id"
```

---

### Task 5: Wire `InformationClient` into the runtime

**Files:**
- Modify: `src/api/main.py` (lifespan)
- Modify: `src/core/registry.py` (or wherever ServiceRegistry lives — locate before editing)
- Modify: `src/workers/__init__.py` (drain task needs the client too)
- Test: `tests/api/test_lifespan.py`, `tests/core/test_registry.py`

- [ ] **Step 1: Locate ServiceRegistry**

```bash
grep -rn "class ServiceRegistry" /home/exedev/watcher/src
```

Note the file path for the next step.

- [ ] **Step 2: Write a failing test that the registry exposes an InformationClient**

```python
# tests/core/test_registry.py
def test_registry_provides_information_client(monkeypatch):
    monkeypatch.setenv("INFORMATION_BASE_URL", "http://localhost:8020")
    monkeypatch.setenv("INFORMATION_API_KEY", "test-key")
    reg = ServiceRegistry()
    client = reg.get_information_client()
    assert client is not None
    assert client._base_url == "http://localhost:8020"
```

- [ ] **Step 3: Extend the constructor + add the accessor**

Extend `ServiceRegistry.__init__` to accept an optional `information_client=None` parameter for test injection. The runtime path uses lazy construction from env:

```python
# src/core/registry.py
class ServiceRegistry:
    def __init__(
        self,
        *,
        fetcher=None,
        extractor_map=None,
        information_client: InformationClient | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._extractor_map = extractor_map
        self._information_client = information_client  # explicit injection wins

    def get_information_client(self) -> InformationClient:
        if self._information_client is None:
            base_url = os.environ.get("INFORMATION_BASE_URL", "http://localhost:8020")
            api_key = os.environ.get("INFORMATION_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "INFORMATION_API_KEY is not set; cannot construct InformationClient"
                )
            self._information_client = InformationClient(base_url=base_url, api_key=api_key)
        return self._information_client

    async def aclose_information_client(self) -> None:
        if self._information_client is not None:
            await self._information_client.aclose()
            self._information_client = None
```

The lifespan pre-warms by calling `reg.get_information_client()` at startup so misconfiguration crashes the API on boot, not on first request. Test injection in Tasks 7 + 12 uses `ServiceRegistry(information_client=fake_sdk, ...)`.

- [ ] **Step 4: Wire into the FastAPI lifespan with explicit shutdown ordering**

`src/api/main.py:46-63` already cancels the worker task and closes Procrastinate. Insert the SDK pre-warm at startup and the close at shutdown **after** the worker task is gathered (workers may have in-flight `get_primary_info_spec()` calls during shutdown):

```python
# src/api/main.py — pseudocode for the lifespan
async with lifespan_context:
    # startup
    reg = get_registry()
    reg.get_information_client()  # pre-warm; raises if INFORMATION_API_KEY missing
    worker_task = asyncio.create_task(start_application_worker(...))
    yield
    # shutdown — strict order
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)
    await proc_app.close_async()
    await reg.aclose_information_client()  # last — workers no longer in flight
```

- [ ] **Step 5: Update Procrastinate `RetryStrategy` to cover SDK exceptions**

The existing `check_watch` retry strategy at `src/workers/tasks.py:51-55` lists `retry_exceptions={ConnectionError, TimeoutError}` (Python builtins). The SDK raises `httpx.ConnectError`, `httpx.TimeoutException`, and the `InformationError` family — none subclass the builtins. Without this fix, an Information service outage marks the job failed on first attempt, no retry.

```python
# src/workers/tasks.py — extend the retry strategy
import httpx
from information_client.errors import ServerError

@bp.task(
    name="check_watch",
    queue="default",
    retry=procrastinate.RetryStrategy(
        max_attempts=3,
        exponential_wait=5,
        retry_exceptions={
            ConnectionError,
            TimeoutError,
            httpx.ConnectError,
            httpx.TimeoutException,
            ServerError,
        },
    ),
)
```

`AuthError`, `NotFound`, and `ValidationError` are NOT in `retry_exceptions` — those are operator-fixable, not transient. (`NotFound` is caught explicitly in Task 7 Step 3 and turned into a skip.)

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/core/test_registry.py tests/api/test_lifespan.py tests/workers/test_tasks.py --no-cov 2>&1 | tail -10
```

Expected: PASS (the workers test confirms RetryStrategy still applies cleanly).

- [ ] **Step 7: Document the env vars**

Update `AGENTS.md` Environment Files section to list `INFORMATION_BASE_URL` (default `http://localhost:8020`) and `INFORMATION_API_KEY` (required).

- [ ] **Step 8: Commit**

```bash
git add src/api/main.py src/core/registry.py src/workers/tasks.py \
    tests/core/test_registry.py tests/api/test_lifespan.py AGENTS.md
git commit -m "#138 feat: wire InformationClient + extend retry strategy for SDK errors"
```

---

### Task 6: `info_resolver` helper — primary InfoSpec resolution with force-refresh retry

**Files:**
- Create: `src/core/info_resolver.py`
- Create: `tests/core/test_info_resolver.py`

- [ ] **Step 1: Write the failing test for happy path**

```python
# tests/core/test_info_resolver.py
async def test_resolve_returns_primary_spec(monkeypatch):
    fake_client = MagicMock()
    fake_spec = MagicMock(info_spec_id="01XYZ", info_item_id="01ABC", document={"target": {"url": "https://x"}})
    fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)
    resolved = await resolve_primary(fake_client, "01ABC")
    assert resolved.info_spec_id == "01XYZ"
    assert resolved.document["target"]["url"] == "https://x"
```

And one for force-refresh:

```python
async def test_resolve_with_force_refresh_passes_flag_through():
    fake_client = MagicMock()
    fake_client.get_primary_info_spec = AsyncMock(return_value=fresh_spec)
    await resolve_primary(fake_client, "01ABC", force_refresh=True)
    fake_client.get_primary_info_spec.assert_awaited_once_with(
        "01ABC", force_refresh=True
    )
```

(Note: the SDK's `force_refresh=True` re-fetches AND overwrites the cache — it does NOT call `invalidate_primary_cache`. Test the flag pass-through, not cache invalidation.)

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/core/test_info_resolver.py --no-cov 2>&1 | tail -5
```

Expected: ImportError or NameError.

- [ ] **Step 3: Verify the SDK's `document` access pattern**

Before writing the resolver, pin the actual access path:

```bash
uv run python3 -c "
from information_client.generated.models.info_spec_out import InfoSpecOut
print([a for a in dir(InfoSpecOut) if 'document' in a.lower() or 'dict' in a.lower()])
print(InfoSpecOut.__annotations__.get('document'))
"
```

The openapi-python-client output exposes a typed wrapper for `document`. Pick whichever the introspection shows: usually `to_dict()` (attrs-style) or `additional_properties`. **Do not guess.**

- [ ] **Step 4: Implement the resolver**

```python
# src/core/info_resolver.py
"""Resolve the primary InfoSpec for a Watch via the Information SDK."""

from dataclasses import dataclass
from information_client import InformationClient


@dataclass(frozen=True)
class ResolvedInfoSpec:
    info_item_id: str
    info_spec_id: str
    document: dict


def _document_to_dict(doc) -> dict:
    """Coerce the SDK's InfoSpecOutDocument wrapper to a plain dict.

    The exact accessor (`to_dict()` vs `additional_properties` vs direct dict)
    is pinned in Step 3; substitute the real one here.
    """
    if hasattr(doc, "to_dict"):
        return dict(doc.to_dict())
    if hasattr(doc, "additional_properties"):
        return dict(doc.additional_properties)
    return dict(doc)


async def resolve_primary(
    client: InformationClient,
    info_item_id: str,
    *,
    force_refresh: bool = False,
) -> ResolvedInfoSpec:
    """Resolve the primary InfoSpec, optionally bypassing the TTL cache."""
    spec = await client.get_primary_info_spec(info_item_id, force_refresh=force_refresh)
    return ResolvedInfoSpec(
        info_item_id=str(spec.info_item_id),
        info_spec_id=str(spec.info_spec_id),
        document=_document_to_dict(spec.document),
    )
```

- [ ] **Step 4: Run tests, confirm PASS**

```bash
uv run pytest tests/core/test_info_resolver.py --no-cov 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add src/core/info_resolver.py tests/core/test_info_resolver.py
git commit -m "#138 feat: info_resolver helper with force_refresh retry"
```

---

### Task 7: Refactor `check_watch` + pipeline + all `watch.url` callsites in workers

**Scope.** Once Task 4 dropped `watch.url`, `src/workers/tasks.py` and `src/workers/pipeline.py` no longer import. This task fixes **all** `watch.url` references in the worker layer in one shot, end-to-end. Notification template variable resolution moves into Task 8; this task only ensures the worker code compiles + emits events with the resolved URL threaded through.

**`watch.url` sites in scope (verified):**
- `src/workers/tasks.py:90` — rate-limit domain extraction.
- `src/workers/tasks.py:93` — fetcher call.
- `src/workers/tasks.py:121` — `WatchEvent(watch_url=watch.url)` for `WATCH_ERROR`.
- `src/workers/tasks.py:162` — `WatchEvent(watch_url=watch.url)` for `WATCH_RECOVERED`.
- `src/workers/tasks.py:175` — `WatchEvent(watch_url=watch.url)` for `CHANGE_DETECTED`.
- `src/workers/pipeline.py:165` — fetch_config read.
- `src/workers/pipeline.py:214` — fetch_config read.
- `src/workers/pipeline.py:343` — `capture_screenshot(watch.url, ...)`.

All five `tasks.py` sites must use the same `resolved.document["target"]["url"]`; the `pipeline.py` callsites take the resolved spec from the parent.

**Files:**
- Modify: `src/workers/tasks.py`
- Modify: `src/workers/pipeline.py` (signature change: `_run_check_pipeline` takes `ResolvedInfoSpec` instead of reading `watch.url`)
- Test: `tests/workers/test_tasks.py` — update fixtures + assertions; add a force-refresh-retry test; add an Information-service-unreachable test.

- [ ] **Step 1: Write a failing test that pipeline resolves URL via the SDK**

Mock the InformationClient to return a known InfoSpec; assert the fetcher is called with that URL and that timeout/render come from `target.fetch.*` (or defaults).

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Refactor `check_watch` — resolve once, thread the URL through**

```python
# src/workers/tasks.py — inside check_watch, after loading the Watch row:
from information_client import NotFound

info_client = reg.get_information_client()
try:
    resolved = await resolve_primary(info_client, str(watch.info_item_id))
except NotFound:
    logger.error(
        "info_item missing for watch — skipping until operator action",
        extra={"watch_id": watch_id, "info_item_id": str(watch.info_item_id)},
    )
    return {"skipped": True, "reason": "info_item_missing"}
# Other SDK errors (ConnectionError, TimeoutError, ServerError, AuthError) are
# intentionally NOT caught here — they propagate to Procrastinate's RetryStrategy.

url = resolved.document["target"]["url"]
fetch_render_value = fetch_render(resolved.document)
fetch_timeout = fetch_timeout_seconds(resolved.document)

fetch_config = {"render": fetch_render_value, "timeout": fetch_timeout}
rate_limit_domain = watch.effective_domain or urlparse(url).hostname or url
async with get_rate_limiter().acquire_for_domain(rate_limit_domain):
    fetch_result = await reg.get_fetcher().fetch(url, config=fetch_config)

# Thread `url` (not `watch.url`) into all event-emission sites:
#   line 121 → WatchEvent(watch_url=url, ..., event_type=WATCH_ERROR)
#   line 162 → WatchEvent(watch_url=url, ..., event_type=WATCH_RECOVERED)
#   line 175 → WatchEvent(watch_url=url, ..., event_type=CHANGE_DETECTED)
```

(Confirm `fetcher.fetch`'s `config` dict accepts `render` — if it doesn't today, either extend it here or pop the unsupported key. `fetch_render` is currently always `False` for prod data, but the contract should be honored.)

- [ ] **Step 4: Pass `resolved` through to `_run_check_pipeline` + delete dead branches**

Pipeline.py currently reads `watch.fetch_config` at lines 165, 214, 343. Each gets distinct treatment:

- **Line 165 (`if ct == "file"` file extractor branch):** Reads `fetch_config.file_format`, `chunk_row_size`, `sort_columns`, `sheet_name`. Since the Task 4 migration aborts on non-HTML content_types, this branch is dead post-cutover. **Delete the entire `if ct == "file":` branch and the `extract_file` import.**
- **Line 214 (`fetch_cfg.get("ignore_patterns")`):** `ignore_patterns` is in the UNSUPPORTED set; the migration aborts if any watch uses it. **Delete the `_apply_ignore_patterns` call** (and the helper if no other caller).
- **Line 343 (`capture_screenshot(watch.url, ...)`):** Replace `watch.url` with the resolved URL threaded down from Task 7's `check_watch`.

For HTML extraction (the only surviving branch), the new shape:

```python
extraction = resolved.document["extraction"]
if extraction["algorithm"] == "css":
    selector = extraction["selector"]  # single string, comma-joined if multi-source
    config = {"selectors": [selector]}  # adapt to existing extractor's config shape
else:  # full_page
    config = {"selectors": []}
chunks = await reg.get_extractor(ContentType.HTML).extract(content, config=config)
```

(`exclude_selectors`, `ignore_selectors`, etc. are gone — the migration aborts on them. The HTML extractor's signature stays the same; it just receives a slimmer config.)

- [ ] **Step 5: Add the force-refresh-retry path (extraction-only re-run)**

When extraction returns zero chunks (or the existing failure-detection criterion), refresh the InfoSpec and **re-run extraction against the same `content`**. We do **not** re-fetch — the working assumption is that selectors changed, not the URL. URL changes propagate on the next scheduled tick.

```python
if extraction_failed(extraction_result):
    resolved = await resolve_primary(
        info_client, str(watch.info_item_id), force_refresh=True
    )
    extraction_result = run_extraction(content, resolved.document)
    # Note: content was fetched against the old InfoSpec's URL. If target.url
    # also changed, the next scheduled tick will fetch from the new URL. We
    # intentionally don't re-fetch here — that's a wider-scope retry.
```

If still failed, emit `WATCH_ERROR` as today.

- [ ] **Step 6: Populate the new Change columns**

When inserting a Change row, set `info_item_id`, `info_spec_id`, `previous_fingerprint` (from the previous snapshot's simhash), `current_fingerprint` (from the current).

- [ ] **Step 7: Update `pipeline.py` line 343 — screenshot uses resolved URL**

`capture_screenshot(watch.url, ...)` becomes `capture_screenshot(resolved_url, ...)` where `resolved_url` is threaded down from `_run_check_pipeline`'s caller. Add a parameter to the pipeline signature if needed.

- [ ] **Step 8: Add the Information-service-unreachable test**

```python
# tests/workers/test_tasks.py
async def test_check_watch_propagates_connection_error(db_session, monkeypatch):
    """A ConnectionError from the SDK propagates so Procrastinate retries."""
    watch = await make_watch(db_session)
    fake_client = MagicMock()
    fake_client.get_primary_info_spec = AsyncMock(
        side_effect=httpx.ConnectError("Information service down")
    )
    reg = ServiceRegistry(fetcher=AsyncMock(), information_client=fake_client)
    monkeypatch.setattr(tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session))
    with pytest.raises(httpx.ConnectError):  # or whatever the SDK surfaces
        await check_watch(str(watch.id), registry=reg)
```

- [ ] **Step 9: Run tests**

```bash
uv run pytest tests/workers/test_tasks.py --no-cov -m "not integration" 2>&1 | tail -10
```

Expected: green (factories from Task 0 already feed valid `info_item_id` values).

- [ ] **Step 10: Commit**

```bash
git add src/workers/tasks.py src/workers/pipeline.py tests/workers/
git commit -m "#138 feat: check_watch + pipeline + screenshot use resolved URL"
```

---

### Task 8: Resolve `watch_url` for every remaining `watch.url`/`watch.fetch_config` callsite

**Scope.** Task 7 fixed worker-layer sites. This task fixes every other site. Run the canonical grep first to confirm completeness:

```bash
grep -rn "watch\.url\|watch\.fetch_config" /home/exedev/watcher/src --include="*.py"
```

Expected before this task: ~12 surviving sites across 4 files. Expected after: 0.

**Files (verified via grep — 6 total source files; tasks 7 covers `src/workers/tasks.py` + `src/workers/pipeline.py`):**
- Modify: `src/core/watches.py` — event-emission sites use resolved URL via SDK.
- Modify: `src/core/notifications/content.py` — template variable resolution; `watch_url` template var receives the resolved URL.
- Modify: `src/api/routes/watches.py` — every read of `watch.url` or `watch.fetch_config`. Watch CRUD endpoint changes are in Task 11; this task only fixes residual reads (e.g. response serialization).
- Modify: `src/api/routes/notification_configs.py` — line 150 `watch.url` read for notification preview.
- Modify: `src/dashboard/routes.py` — multiple `watch.url` reads (lines 427, 896, 932, 2413 — verify by grep at task time): screenshot trigger, notification preview event, ad-hoc test send. All resolve URL via the request-scoped SDK.
- Modify: `src/dashboard/templates/pages/watch_detail.html` — `{{ watch.url }}` → `{{ resolved_url }}`.
- Test: existing notification + dashboard tests get fixture updates; add tests asserting the resolved URL ends up in templates and notification events.

- [ ] **Step 1: Write a failing test that notification dispatch reads URL from primary InfoSpec**

Mock the SDK; assert `event.watch_url` equals the InfoSpec's `target.url`, not whatever the Watch row holds.

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Add a small helper to resolve a Watch's current URL**

```python
# src/core/watches.py
async def resolve_watch_url(watch: Watch, client: InformationClient) -> str:
    resolved = await resolve_primary(client, str(watch.info_item_id))
    return resolved.document["target"]["url"]
```

- [ ] **Step 4: Update event emission**

Replace `watch_url=watch.url` with `watch_url=await resolve_watch_url(watch, info_client)` in `src/core/watches.py` and `src/workers/tasks.py`.

- [ ] **Step 5: Update the dashboard detail view**

Pass `resolved_url` to the template via the route handler; update `pages/watch_detail.html` to render `{{ resolved_url }}`.

- [ ] **Step 6: Verify zero remaining `watch.url`/`watch.fetch_config` reads**

```bash
grep -rn "watch\.url\|watch\.fetch_config" /home/exedev/watcher/src --include="*.py"
```

Expected: empty output. If anything matches, address it before moving on.

- [ ] **Step 7: Run the full unit suite**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -3
```

Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/core/watches.py src/core/notifications/ \
    src/api/routes/watches.py src/api/routes/notification_configs.py \
    src/dashboard/routes.py src/dashboard/templates/pages/watch_detail.html \
    tests/
git commit -m "#138 feat: resolve watch_url from primary InfoSpec at every callsite"
```

---

### Task 9: Refine drain envelope + switch partition key to `info_item_id`

**Files:**
- Modify: `src/workers/changes_drain.py`
- Test: `tests/workers/test_changes_drain.py`

- [ ] **Step 1: Write a failing test for the new envelope shape**

```python
async def test_envelope_includes_info_item_and_fingerprints(db_session):
    # Required FKs on Change: watch_id, previous_snapshot_id, current_snapshot_id.
    # Build the parent rows via the helpers from Task 0.
    watch = await make_watch(db_session)
    prev_snap = await make_snapshot(db_session, watch_id=watch.id)
    curr_snap = await make_snapshot(db_session, watch_id=watch.id)
    change = Change(
        watch_id=watch.id,
        previous_snapshot_id=prev_snap.id,
        current_snapshot_id=curr_snap.id,
        info_item_id=watch.info_item_id,
        info_spec_id=ULID(),  # synthetic for the test; FK not enforced unless you add one
        previous_fingerprint=12345,
        current_fingerprint=67890,
        detected_at=datetime.now(UTC),
    )
    db_session.add(change)
    await db_session.flush()
    payload = _build_envelope(change)
    body = json.loads(payload)
    assert body["schema_version"] == 2
    assert body["info_item_id"] == str(change.info_item_id)
    assert body["info_spec_id"] == str(change.info_spec_id)
    assert body["previous_fingerprint"] == 12345
    assert body["current_fingerprint"] == 67890
```

And a partition-key test:

```python
async def test_drain_uses_info_item_id_as_partition_key(...):
    # publish via fakeredis, assert the message's `key` field equals str(info_item_id).
```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Update `_build_envelope`, partition key, and bump `schema_version`**

```python
def _build_envelope(change) -> bytes:
    return json.dumps({
        "schema_version": 2,  # was 1; partition key + payload shape change
        "change_id": str(change.id),
        "watch_id": str(change.watch_id),
        "info_item_id": str(change.info_item_id),
        "info_spec_id": str(change.info_spec_id),
        "previous_snapshot_id": (
            str(change.previous_snapshot_id) if change.previous_snapshot_id else None
        ),
        "current_snapshot_id": str(change.current_snapshot_id),
        "previous_fingerprint": change.previous_fingerprint,
        "current_fingerprint": change.current_fingerprint,
        "detected_at": format_utc_iso(change.detected_at),
        "significance": change.significance,
        "visual_change_score": change.visual_change_score,
        "metadata": change.change_metadata,
    }).encode("utf-8")

# In drain_changes_outbox:
key=str(change.info_item_id),  # was change.watch_id
headers={"schema_version": "2"},  # match the envelope's schema_version
```

Note: there are no production consumers of the v1 envelope yet — `tools/info_changes_consumer.py` is the only consumer and is reference code that ignores schema_version. Bumping is a forward-compatibility marker for Phase 3+ Archive.

- [ ] **Step 4: Add `pg_try_advisory_xact_lock` at drain start**

```python
# src/workers/changes_drain.py — inside drain_changes_outbox, before select_unpublished
DRAIN_ADVISORY_LOCK_ID = 0xCDA1  # phase 2c drain lock; arbitrary constant

async with get_session_factory()() as session:
    locked = await session.scalar(
        sa.select(sa.func.pg_try_advisory_xact_lock(DRAIN_ADVISORY_LOCK_ID))
    )
    if not locked:
        logger.info("drain_changes_outbox skipped — another drain holds the lock")
        return {"published": 0, "failed": 0, "skipped": True}
    # ...existing body...
```

The lock auto-releases at transaction end. Concurrent drains see `locked=False` and return cleanly.

- [ ] **Step 5: Add a test for the advisory-lock skip path**

```python
async def test_drain_skips_when_lock_held(db_session, fake_redis_publisher):
    """If pg_try_advisory_xact_lock returns false, drain returns early."""
    # Open a separate session that grabs the lock first
    async with get_session_factory()() as holder:
        await holder.execute(
            sa.text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": DRAIN_ADVISORY_LOCK_ID},
        )
        result = await drain_changes_outbox(batch_size=10)
        assert result == {"published": 0, "failed": 0, "skipped": True}
        await holder.execute(
            sa.text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": DRAIN_ADVISORY_LOCK_ID},
        )
```

- [ ] **Step 6: Run drain tests**

```bash
uv run pytest tests/workers/test_changes_drain.py --no-cov 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```bash
git add src/workers/changes_drain.py tests/workers/test_changes_drain.py
git commit -m "#138 feat: drain envelope v2 with info_item_id + advisory lock"
```

---

### Task 10: Schedule `drain_changes_outbox` via `@bp.periodic`

**Why this differs from the prior plan.** Procrastinate exposes periodic via the **decorator** `@bp.periodic(cron="...", periodic_id="...")` stacked above `@bp.task(...)` — it does **not** expose `app.add_periodic_task(...)` (verified via `hasattr(app, 'add_periodic_task') == False` on procrastinate 3.7.2). Pattern is already used in `src/workers/tasks.py:188` for `schedule_tick`. Cadence floor is 1 minute (5-field cron via croniter).

**Files:**
- Modify: `src/workers/changes_drain.py` (add the decorator)
- Test: `tests/workers/test_app.py`

- [ ] **Step 1: Write a failing test for the periodic registration**

```python
def test_drain_changes_outbox_registered_as_periodic():
    app = get_app()
    periodic_names = {p.task.name for p in app.periodic_registry.periodic_tasks.values()}
    assert "drain_changes_outbox" in periodic_names
```

- [ ] **Step 2: Run, confirm failure**

- [ ] **Step 3: Stack `@bp.periodic` above the existing `@bp.task`**

```python
# src/workers/changes_drain.py — replace the existing @bp.task decorator stack
@bp.periodic(cron="* * * * *", periodic_id="drain_changes_outbox")
@bp.task(name="drain_changes_outbox", queue="default")
async def drain_changes_outbox(*, batch_size: int = 100, **periodic_kwargs) -> dict:
    """..."""
```

Procrastinate passes `timestamp` as a kwarg to periodic tasks; absorb it via `**periodic_kwargs`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/workers/test_app.py --no-cov 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add src/workers/changes_drain.py tests/workers/test_app.py
git commit -m "#138 feat: drain_changes_outbox runs every minute via @bp.periodic"
```

---

### Task 11: Dashboard + API — Watch CRUD takes `info_item_id`

**Scope.** Minimum viable: forms accept `info_item_id`; create endpoint validates it exists. No inline InfoItem creation; no editing of the InfoSpec from the Watch UI. **Substantial additional rework:** every `fetch_config`-source row in `WATCH_FIELD_META` and every inline-edit POST endpoint that mutated `watch.url` or `watch.fetch_config` must be removed since those columns are gone after Task 4. These edits live on the InfoSpec now and are managed via the Information service, not the Watcher dashboard.

**`WATCH_FIELD_META` rows to drop (verified at `src/dashboard/routes.py:550-700`):**
`url`, `timeout` (`fetch_config.timeout`), `headers`, `ignore_patterns`, `selectors`, `exclude_selectors`, `dynamic_id_patterns`, `strip_boilerplate`, `skip_empty_pages`, `file_format`, `chunk_row_size`, `sort_columns`, `sheet_name`, `viewport_width`, `viewport_height`. **Keep:** `name`, `interval`, `tags`, `description`, anything else with `source != "fetch_config"` and `key != "url"`.

A read-only `resolved_url` row is added for visibility (renders the SDK-resolved URL, no edit handle).

**Files:**
- Modify: `src/api/schemas/watch.py` (Pydantic)
- Modify: `src/api/routes/v1.py` (or the actual Watch CRUD module — locate first)
- Modify: `src/dashboard/routes.py` — `WATCH_FIELD_META` slimming + Watch create/edit handlers + drop the inline POST endpoints for fetch_config fields.
- Modify: `src/dashboard/templates/pages/watch_form.html` (single template covering create + edit; there is no separate `watch_create.html` — verified)
- Modify: `src/dashboard/templates/pages/watch_detail.html`
- Modify: any `partials/watch_field*.html` that referenced dropped fields
- Create: `src/dashboard/templates/partials/info_item_picker.html`
- Test: `tests/api`, `tests/dashboard`

- [ ] **Step 1: Locate Watch CRUD endpoints + inline-field mutation endpoints**

```bash
grep -rn "POST.*watches\|create.*watch" /home/exedev/watcher/src/api/routes
grep -rln "WATCH_FIELD_META\|watch_field\|watch_form" /home/exedev/watcher/src
```

Note every route that posts to a `fetch_config` field — they all become 410 Gone or get removed entirely.

- [ ] **Step 2: Write failing tests for the new shape**

API: `POST /api/v1/watches` with `{name, info_item_id, content_type}` succeeds; without `info_item_id` returns 422; with non-existent `info_item_id` returns 422 (validated via the SDK).

Dashboard:
- GET `/watches/new` renders the InfoItem picker.
- POST with a chosen `info_item_id` redirects to the new watch's detail page.
- Inline POST to (e.g.) `/watches/<id>/field/url/edit` returns 404 or 405 (gone).
- Watch detail page shows `resolved_url`, no edit handle.

- [ ] **Step 3: Update the Pydantic schemas**

```python
class WatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    info_item_id: str  # ULID validation downstream
    content_type: ContentType
    description: str | None = None
    tags: list[str] | None = None
    schedule_config: dict = Field(default_factory=dict)
```

(No `url`, no `fetch_config`.)

- [ ] **Step 4: Update the API route**

Accept the new schema. Validate `info_item_id` via `await client.get_info_item(info_item_id)` (the SDK exposes a GET, not a HEAD). Translate errors:

- `NotFound` → HTTP 422 with `{"detail": "info_item_id <id> does not exist"}`.
- `httpx.ConnectError` / `httpx.TimeoutException` / `ServerError` → HTTP 503 with `Retry-After: 30` header. The Information service is briefly unavailable; the operator's UI submission will retry.
- `AuthError` → HTTP 500 (this is a Watcher misconfiguration; surface loudly).

- [ ] **Step 5: Update the dashboard create handler**

The route fetches `await client.list_info_items()` and passes the list to the template. The template renders a `<select>` populated from it. If the list is empty, render a message linking to the Information service.

- [ ] **Step 6: Slim `WATCH_FIELD_META`**

Remove every row whose `"source"` is `"fetch_config"`, plus the `url` row. Add a synthetic read-only `resolved_url` row that displays the value passed in template context (resolved via the SDK in Task 8).

- [ ] **Step 7: Drop inline-edit POST endpoints for the dropped fields**

Each was a route that mutated `watch.fetch_config[<key>]` and rerendered a partial. Delete these routes; remove the associated partials or simplify them to display-only.

- [ ] **Step 8: Create the picker partial**

```jinja
{# src/dashboard/templates/partials/info_item_picker.html #}
<label class="form-label" for="info_item_id">Information Item</label>
<select name="info_item_id" id="info_item_id" class="form-input" required>
  <option value="" disabled selected>Choose an Information Item…</option>
  {% for item in info_items %}
  <option value="{{ item.info_item_id }}">{{ item.name }}</option>
  {% endfor %}
</select>
{% if not info_items %}
<p class="text-sm text-warning">No InfoItems yet — create one in the Information service first.</p>
{% endif %}
```

- [ ] **Step 9: Update the watch detail template**

Replace `{{ watch.url }}` with `{{ resolved_url }}` (populated by Task 8). Confirm no remaining template references `watch.url` or `watch.fetch_config`:

```bash
grep -rn "watch\.url\|watch\.fetch_config" /home/exedev/watcher/src/dashboard/templates
```

Expected: empty.

- [ ] **Step 10: Run all tests**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 11: Manual smoke in the dev server**

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload &
# Visit https://watcher.exe.xyz:8001/watches/new
# Verify: picker renders, submission works, detail page shows resolved URL.
```

- [ ] **Step 12: Commit**

```bash
git add src/api/ src/dashboard/ tests/
git commit -m "#138 feat: Watch CRUD accepts info_item_id; drop url + fetch_config from UI"
```

---

### Task 12: End-to-end verification

**Why split.** A single pytest that chains a real check_watch run + drain + XREADGROUP needs three live subsystems (Postgres with both schemas, Redis, real or in-process Information service) plus a deterministic fetcher. That's brittle as a unit-suite member. We do **two** smaller things instead:
1. A worker-level integration test that exercises check_watch → Change row → drain → fakeredis stream, mocking the SDK.
2. A `scripts/smoke_phase2c.sh` walkthrough that exercises the same path against the real running services (Information on 8020, Watcher on 8001, Redis on 6379).

**Files:**
- Create: `tests/workers/test_phase2c_drain_integration.py` (mock SDK, real DB, fakeredis)
- Create: `scripts/smoke_phase2c.sh`

- [ ] **Step 1: Worker-level integration test**

```python
# tests/workers/test_phase2c_drain_integration.py
@pytest.mark.integration
async def test_check_to_drain_to_stream(db_session, monkeypatch, fake_redis_publisher):
    """check_watch produces a Change with info_item_id; drain publishes envelope v2."""
    # Make a Watch + InfoItem + InfoSpec via the factories
    watch = await make_watch(db_session, url="https://example.com")
    # Mock fetcher to return known content
    fake_fetch = AsyncMock(return_value=fetch_result_with_known_content)
    # Mock the SDK to return the InfoSpec inserted by make_watch
    fake_sdk = make_fake_sdk_returning(watch.info_item_id, "https://example.com")
    reg = ServiceRegistry(fetcher=fake_fetch, information_client=fake_sdk)
    monkeypatch.setattr(tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session))
    # Execute
    await check_watch(str(watch.id), registry=reg)
    # Verify: a Change row exists with info_item_id populated
    change = (await db_session.execute(select(Change))).scalar_one()
    assert change.info_item_id == watch.info_item_id
    assert change.current_fingerprint is not None
    # Drain
    monkeypatch.setattr("src.workers.changes_drain.ChangePublisher",
                        lambda: fake_redis_publisher)
    result = await drain_changes_outbox(batch_size=10)
    assert result["published"] == 1
    # Inspect the published envelope
    msg = fake_redis_publisher.last_published
    assert msg["key"] == str(watch.info_item_id)
    body = json.loads(msg["payload"])
    assert body["schema_version"] == 2
    assert body["info_item_id"] == str(watch.info_item_id)
    assert body["info_spec_id"]
    assert body["current_fingerprint"]
```

- [ ] **Step 2: Smoke script for the running services**

```bash
# scripts/smoke_phase2c.sh
#!/usr/bin/env bash
set -euo pipefail
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)

# 1. Create InfoItem + InfoSpec via the Information service
ITEM=$(curl -fsS -X POST -H "X-API-Key: $INFORMATION_API_KEY" \
    -H "content-type: application/json" \
    -d '{"name": "Smoke 2c"}' \
    "${INFORMATION_BASE_URL:-http://localhost:8020}/api/v1/info-items" | jq -r .info_item_id)
curl -fsS -X POST -H "X-API-Key: $INFORMATION_API_KEY" \
    -H "content-type: application/json" \
    -d "{\"document\": {\"schema_version\": 1, \"target\": {\"url\": \"https://example.com\"}, \"extraction\": {\"algorithm\": \"full_page\"}, \"fingerprint\": {\"algorithm\": \"simhash\"}}}" \
    "${INFORMATION_BASE_URL:-http://localhost:8020}/api/v1/info-items/$ITEM/info-specs" >/dev/null

# 2. Create a Watch referencing it (Watcher API also requires X-API-Key)
WATCH=$(curl -fsS -X POST \
    -H "X-API-Key: ${WATCHER_API_KEY:?WATCHER_API_KEY env var required for smoke}" \
    -H "content-type: application/json" \
    -d "{\"name\": \"Smoke 2c\", \"info_item_id\": \"$ITEM\", \"content_type\": \"html\"}" \
    "http://localhost:8001/api/v1/watches" | jq -r .id)
echo "watch_id=$WATCH info_item_id=$ITEM"

# 3. Tail the consumer in another shell, confirm an envelope arrives:
#    uv run python tools/info_changes_consumer.py --group smoke --max-messages 1
```

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest --no-cov -m "not integration" 2>&1 | tail -5
uv run pytest -m integration --no-cov 2>&1 | tail -5
uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 4: Manual smoke**

Run `bash scripts/smoke_phase2c.sh` against the dev services. Confirm reference consumer JSONL output contains `info_item_id`, `info_spec_id`, `schema_version: 2`, and the fingerprint fields.

- [ ] **Step 5: Documentation sweep**

Update:
- `AGENTS.md` — Watch creation requires an InfoItem; document `INFORMATION_BASE_URL` + `INFORMATION_API_KEY`; note that Phase 2c renamed the envelope to v2.
- `docs/COMMANDS.md` — any commands that referenced `watch.url`.
- `README.md` — update the "what is a Watch" sentence if it mentions URLs.

- [ ] **Step 6: Final commit + merge to main**

```bash
git add -A
git commit -m "#138 docs: Phase 2c — InfoItem-native Watch documentation"
# Per project memory: merge feature branches to main locally
git checkout main
git merge --no-ff feat/138-phase2c-cutover
sudo systemctl restart watcher
```

---

## Wrap-up

After Task 12:
- Every Watch has `info_item_id`; `url` and `fetch_config` are gone.
- `check_watch` resolves URL + fetch defaults from the primary InfoSpec via the SDK at every check; force-refresh on extraction failure.
- The drain envelope is `schema_version: 2` and carries `info_item_id`, `info_spec_id`, and pre/post fingerprints; partition key is `info_item_id`.
- `drain_changes_outbox` runs every minute via `@bp.periodic`, guarded by `pg_try_advisory_xact_lock` against concurrent runs.
- Dashboard Watch creation uses an InfoItem picker; all `fetch_config` inline editors are gone.

**Open follow-ups (deferred):**
- Inline InfoItem creation from the Watch form.
- Multi-selector InfoSpec extension (`extraction.selector` becomes a list with proper semantics).
- PDF/file extraction algorithm enum extension.
- InfoSpec schema extension for the `fetch_config` knobs we currently fail-fast on (headers, ignore_patterns, viewport_*, etc.).
- Sub-minute drain cadence (asyncio loop or dedicated worker if needed).
- Archive consumer (Phase 3+) consuming `info.changes`.
- Operational hardening of `info_changes_consumer.py` (retry/backoff for production use, per CR round 5 #31).

**Risk register:**
- **Cross-schema FK:** Postgres-specific. If we ever migrate to a different DB, this needs revisiting.
- **TTL cache staleness:** 60-second window between InfoSpec updates and Watcher noticing. Acceptable for prototype; revisit if extraction-failure rates climb.
- **1-minute drain cadence:** if the Watcher worker is partitioned from Redis, unpublished rows back up. The drain catches up on reconnect (the advisory lock means the next tick after recovery picks up everything in one batch). Operationally: monitor `published_to_bus_at IS NULL` row count via a DB query.
- **Information service unavailability:** check_watch retries via Procrastinate's existing RetryStrategy. After 3 attempts, the job is dead-lettered; the watch's next scheduled tick retries. No `WATCH_ERROR` event is emitted for infrastructure outages — only target-fetch failures.
- **Migration is not idempotent on a partially populated DB:** the data backfill assumes `info_item_id IS NULL`. If interrupted mid-loop, re-running creates duplicate InfoItems. Wrap the migration body in a single transaction (default for alembic) and don't catch exceptions inside the loop.
