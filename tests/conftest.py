"""Shared test fixtures — async database session and FastAPI TestClient.

tests/fixtures/ holds static sample files used by extractor tests (e.g. sample.html).

Factory contract (#185 Phase A)
--------------------------------
The module-level async helpers ``make_watch``, ``make_info_item``,
``make_info_source``, and ``bind_primary_source`` are NOT pytest fixtures —
they are awaitable factory functions test code can call directly.

``make_watch`` takes an optional ``info_item_id`` and ``watched_item``.
A parent ``WatchedItem`` is auto-created (or attached) to honour the 1:1
``watched_items.info_item_id`` uniqueness constraint. The legacy
``target_info_source_id`` / ``schedule_config`` columns are gone.

Archiver v4.0.0: sub_aspect concept removed — ``bind_sub_aspect`` deleted;
``make_info_source`` no longer accepts ``parent_info_source_id``.

Phase 5 (#156): ``make_snapshot`` and ``default_snapshot_fixture`` removed —
Snapshot table dropped. ``InfoSpec`` table and ``make_info_spec`` factory
also dead-code-removed under #160.
"""

import logging
import os
import re
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest
from archiver_client import ArchiverClient, NotFound
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.api.deps import get_db_session, get_probe_fn, require_api_key
from src.core.models import Base
from src.core.models.app_user import AppUser
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem
from src.core.probe import ProbeResult
from src.core.registry import ServiceRegistry, set_registry_for_testing
from src.dashboard.deps import get_dashboard_user
from tests._information_test_models import (
    InfoItem,  # noqa: F401  registers mapper
    InfoItemSource,
    InfoSource,  # noqa: F401  registers mapper
)

logger = logging.getLogger(__name__)

ARCHIVER_REPO_PATH = Path(os.environ.get("ARCHIVER_REPO_PATH", "/home/exedev/archiver"))

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL environment variable is not set. "
        "Load env: export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)"
    )


def _make_mock_probe():
    """Return a mock probe that resolves URLs without real HTTP calls."""

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


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# Requires the ``revision: str`` PEP 526 annotation that the modern
# alembic generator emits. Older or hand-edited version files without the
# annotation cause ``_archiver_alembic_head`` to return None and the caller
# falls through to the subprocess invocation — still correct, just no cache
# benefit.
_ALEMBIC_REVISION_RE = re.compile(r'^revision:\s*str\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_ALEMBIC_DOWN_REVISION_RE = re.compile(r'^down_revision:[^=]*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _archiver_alembic_head() -> str | None:
    """Return Archiver's HEAD alembic revision, or ``None`` if undetectable.

    Walks ``alembic/versions/*.py`` and identifies the leaf revision (the
    one no other revision points back to via ``down_revision``). Pure
    file-parse — no Archiver imports, no subprocess, sub-millisecond.

    Returns ``None`` rather than raising when the migrations directory is
    missing or empty so the caller can fall through to the existing
    subprocess invocation (which has its own clearer error message).
    """
    versions_dir = ARCHIVER_REPO_PATH / "alembic" / "versions"
    if not versions_dir.is_dir():
        return None

    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for path in versions_dir.glob("*.py"):
        try:
            text_content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rev_match = _ALEMBIC_REVISION_RE.search(text_content)
        if not rev_match:
            continue
        revisions.add(rev_match.group(1))
        down_match = _ALEMBIC_DOWN_REVISION_RE.search(text_content)
        if down_match:
            down_revisions.add(down_match.group(1))

    heads = revisions - down_revisions
    if len(heads) != 1:
        # Empty (no migrations) or branched (multi-head) — let caller fall through.
        return None
    return heads.pop()


def _to_sync_url(database_url: str) -> str:
    """Translate an async SQLAlchemy URL to a sync one for the cache probe.

    The probe is a one-shot ``SELECT version_num`` — async machinery would
    add startup cost we're explicitly trying to avoid. ``psycopg`` (v3) is
    a project dependency via ``procrastinate[psycopg]``, so it's always
    available.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+asyncpg://") :]
    return database_url


def _information_schema_at_revision(database_url: str, expected_revision: str) -> bool:
    """Return True iff ``information.alembic_version`` already holds ``expected_revision``.

    Cheap pre-check: a single ``SELECT version_num`` against the test DB.
    Returns False on any error (missing schema, missing table, connection
    refused, multiple rows) so the caller falls through to the full
    subprocess invocation. Never raises — test setup must not crash on a
    cache-probe failure.
    """
    sync_url = _to_sync_url(database_url)
    engine = None
    try:
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM information.alembic_version"))
            rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001 - pre-check must never crash setup
        logger.debug("archiver alembic cache probe failed: %s", exc)
        return False
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001
                pass

    if len(rows) != 1:
        return False
    return rows[0][0] == expected_revision


def _apply_archiver_migrations(database_url: str) -> None:
    """Run the Archiver service's alembic migrations against ``database_url``.

    The `information` schema is owned in production by the sibling Archiver
    repo (`/home/exedev/archiver`). Watcher tests need real `info_sources` /
    `info_specs` / `info_items` tables because conftest helpers
    (``make_info_item``, ``make_info_source``, ``bind_primary_source``, etc.)
    write ``information.*`` rows that the ``info_client`` mock fixture reads
    back via ``InfoItemSource`` queries.  We invoke archiver's own alembic
    instead of mirroring the schema in ``tests/_information_test_models.py``
    — that way schema drift is impossible: the same migrations that build prod
    build the test schema.

    Cache-check (#150): if ``information.alembic_version`` already matches
    Archiver's HEAD, skip the subprocess entirely. Saves the ~1-2 s
    ``uv run alembic`` cold start on warm test sessions. The companion
    teardown in ``test_engine`` no longer drops the ``information``
    schema, so warm reruns hit the cache.
    """
    if not (ARCHIVER_REPO_PATH / "alembic.ini").is_file():
        raise RuntimeError(
            f"Archiver repo not found at {ARCHIVER_REPO_PATH}. "
            "Set ARCHIVER_REPO_PATH or clone the sibling repo."
        )

    head_revision = _archiver_alembic_head()
    if head_revision is not None and _information_schema_at_revision(database_url, head_revision):
        logger.debug(
            "archiver schema already at HEAD %s — skipping alembic subprocess",
            head_revision,
        )
        return

    env = {**os.environ, "ARCHIVER_DATABASE_URL": database_url}
    try:
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=str(ARCHIVER_REPO_PATH),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"archiver alembic upgrade failed (exit {e.returncode}):\n"
            f"--- stdout ---\n{e.stdout}\n"
            f"--- stderr ---\n{e.stderr}"
        ) from e


@pytest.fixture(scope="session")
async def test_engine():
    # Build the `information` schema by running Archiver's own alembic
    # migrations against TEST_DATABASE_URL. Single source of schema truth.
    _apply_archiver_migrations(TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Restrict ``create_all`` to public-schema watcher tables — the
        # ``information`` schema is owned by Archiver's alembic above.
        watcher_tables = [t for t in Base.metadata.sorted_tables if t.schema in (None, "public")]
        await conn.run_sync(Base.metadata.create_all, tables=watcher_tables)
        # Phase 5 (#156): trg_changes_update_last_changed_at trigger removed.
        # No triggers to recreate.
    yield engine
    async with engine.begin() as conn:
        # Drop only public-schema watcher tables; the `information` schema
        # is intentionally left alive so the next pytest session's
        # ``_apply_archiver_migrations`` cache-check (#150) finds an
        # already-current ``information.alembic_version`` and skips the
        # ~1-2 s ``uv run alembic`` subprocess. Per-test data isolation
        # for ``information.*`` rows is handled by ``db_session``'s
        # savepoint rollback, so leaving the empty schema in place is
        # safe between sessions.
        watcher_tables = [t for t in Base.metadata.sorted_tables if t.schema in (None, "public")]
        await conn.run_sync(Base.metadata.drop_all, tables=watcher_tables)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    async with test_engine.connect() as conn:
        txn = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)

        # Start a savepoint; route code calling session.commit() will
        # commit only this savepoint, not the outer real transaction.
        nested = await conn.begin_nested()

        @event.listens_for(session.sync_session, "after_transaction_end")
        def restart_savepoint(db_session, transaction):
            nonlocal nested
            if not nested.is_active:
                nested = conn.sync_connection.begin_nested()

        yield session

        await session.close()
        await txn.rollback()


# ---------------------------------------------------------------------------
# Module-level async factories (NOT pytest fixtures).
#
# Tests call these directly:  ``watch = await make_watch(db_session, name="X")``
# ---------------------------------------------------------------------------


async def make_info_item(session, *, name="Test Item", description=None):
    """Create and flush an InfoItem row."""
    item = InfoItem(name=name, description=description)
    session.add(item)
    await session.flush()
    return item


async def make_info_source(
    session,
    *,
    url="https://example.com",
    source_specs=None,
):
    """Create and flush an InfoSource row (Archiver v4.0.0 shape).

    ``source_specs`` is a list of extraction/fingerprint spec dicts following
    the Archiver format ``[{schema_version, extraction, fingerprint}]``.
    Defaults to a single full-page/simhash spec when omitted.
    """
    if source_specs is None:
        source_specs = [
            {
                "schema_version": 1,
                "extraction": {"algorithm": "full_page"},
                "fingerprint": {"algorithm": "simhash"},
            }
        ]
    source = InfoSource(url=url, source_specs=source_specs)
    session.add(source)
    await session.flush()
    return source


async def bind_primary_source(session, *, info_item_id, info_source_id):
    """Insert a binding into information.info_item_sources (Archiver v4.0.0: no role)."""
    session.add(
        InfoItemSource(
            info_item_id=info_item_id,
            info_source_id=info_source_id,
        )
    )
    await session.flush()


async def make_watch(
    session,
    *,
    name="Test Watch",
    info_item_id=None,
    watched_item=None,
    primary_url="https://example.com",
    **kwargs,
):
    """Construct a Watch tied to a WatchedItem + InfoItem (#185 Phase A shape).

    When ``info_item_id`` is not supplied, an InfoItem + primary InfoSource +
    binding are auto-created — except when ``watched_item`` is supplied, in
    which case ``info_item_id`` defaults to ``watched_item.info_item_id`` so
    callers don't have to repeat it. When ``watched_item`` is not supplied,
    a fresh WatchedItem is auto-created (or attaches to an existing one for
    the same info_item_id).

    Extra ``**kwargs`` flow into the Watch constructor (tags, description,
    content_type, etc.). Pass ``domain_name=`` to set WatchedItem.domain_name.
    Note: ``schedule_config`` no longer lives on Watch (moved to
    WatchedItem.default_schedule_config). Use ``primary_url=`` to seed the
    auto-created InfoSource's URL. ``target_info_source_id`` removed (Archiver
    v4.0.0: sub_aspect concept eliminated).
    """
    # domain_name lives on WatchedItem, not Watch — extract before passing to Watch.
    domain_name = kwargs.pop("domain_name", None)

    if info_item_id is None and watched_item is not None:
        # Default to the WatchedItem's InfoItem so the assertion below can't
        # trip on an auto-created mismatch.
        info_item_id = watched_item.info_item_id

    if info_item_id is None:
        item = await make_info_item(session)
        info_item_id = item.info_item_id
        primary = await make_info_source(session, url=primary_url)
        await bind_primary_source(
            session,
            info_item_id=info_item_id,
            info_source_id=primary.info_source_id,
        )

    if watched_item is None:
        # Attach to existing WatchedItem for this info_item_id if present;
        # otherwise create a fresh one. The 1:1 uniqueness on info_item_id
        # would otherwise fail when two Watches share an InfoItem.
        existing = (
            await session.execute(
                select(WatchedItem).where(WatchedItem.info_item_id == info_item_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            watched_item = existing
            if not watched_item.effective_url and primary_url:
                watched_item.effective_url = primary_url
                await session.flush()
        else:
            watched_item = WatchedItem(
                info_item_id=info_item_id,
                name=f"WI for {name}",
                effective_url=primary_url,
            )
            session.add(watched_item)
            await session.flush()
    elif watched_item.info_item_id != info_item_id:
        raise AssertionError(
            f"watched_item.info_item_id ({watched_item.info_item_id}) "
            f"must match info_item_id ({info_item_id})"
        )

    # Extract cascade-suspend flag before passing kwargs to Watch constructor.
    # Accepts both "domain_suspended" (legacy) and "suspended_by_domain" (current).
    suspended = kwargs.pop("suspended_by_domain", kwargs.pop("domain_suspended", False))

    watch_kwargs = {
        "name": name,
        "watched_item_id": watched_item.id,
        **kwargs,
    }
    if suspended:
        watch_kwargs["suspended_by_domain"] = True
    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()

    # Apply domain_name to the WatchedItem if provided (and not already set).
    # Auto-create the Domain row if it doesn't exist (FK requires it).
    if domain_name is not None and watched_item.domain_name is None:
        existing_domain = (
            await session.execute(select(Domain).where(Domain.name == domain_name))
        ).scalar_one_or_none()
        if existing_domain is None:
            session.add(Domain(name=domain_name))
            await session.flush()
        watched_item.domain_name = domain_name
        await session.flush()

    # Propagate cascade-suspend to WatchedItem (mirrors domain-deactivation cascade).
    if suspended and not watched_item.domain_suspended:
        watched_item.domain_suspended = True
        await session.flush()

    # Eager-populate the watched_item relationship so callers can read
    # watch.watched_item without a separate await. The model declares
    # lazy="joined" but `flush()` alone doesn't trigger the join.
    await session.refresh(watch, ["watched_item"])
    return watch


# ---------------------------------------------------------------------------
# Legacy pytest-fixture variants (renamed to avoid name collision with the
# module-level helpers above). Tests that consume them as fixtures keep
# working: ``def test_x(default_watch_fixture)``.
# ---------------------------------------------------------------------------
# Phase 5 (#156): make_snapshot + default_snapshot_fixture removed.
# Snapshot table dropped; tests that used them are also removed.


@pytest.fixture
def info_client(db_session, request):
    """Mock ArchiverClient backed by the test DB's ``information.*`` tables.

    Routes pull the SDK via ``get_registry().get_archiver_client()``.
    This fixture swaps the registry singleton's cached client for an AsyncMock
    whose methods look up live rows in ``db_session`` where possible.

    Phase 5: ``information.info_specs`` no longer exists in the Archiver schema.
    ``get_primary_info_spec`` now returns a synthesized stub spec for any
    info_item_id — tests that need a specific URL should stub
    ``info_client.get_primary_info_spec`` directly.

    Tests that need to exercise SDK error paths can stub individual methods
    on the returned mock
    (e.g. ``info_client.get_primary_info_spec.side_effect = NotFound``).
    """
    fake_client = MagicMock(spec=ArchiverClient)

    async def _list_info_items():
        result = await db_session.execute(select(InfoItem))
        items = result.scalars().all()
        out = []
        for item in items:
            entry = MagicMock()
            entry.info_item_id = str(item.info_item_id)
            entry.name = item.name
            entry.description = item.description
            entry.owner = None
            entry.created_at = item.created_at or datetime.now(UTC)
            entry.updated_at = item.updated_at or datetime.now(UTC)
            out.append(entry)
        return out

    async def _get_primary_info_spec(info_item_id: str, *, force_refresh: bool = False):
        # Phase 5: info_specs table is gone from the Archiver schema.
        # Return a synthesised stub spec so route handlers can resolve a URL
        # without a real InfoSpec row. Tests that need a specific URL should
        # stub this method directly on the returned fake_client.
        out = MagicMock()
        out.info_item_id = info_item_id
        out.info_spec_id = "01TESTSPEC00000000000000XX"
        doc = MagicMock()
        doc.to_dict = MagicMock(
            return_value={
                "schema_version": 1,
                "target": {"url": "https://example.com/page"},
                "extraction": {"algorithm": "full_page"},
                "fingerprint": {"algorithm": "simhash"},
            }
        )
        out.document = doc
        return out

    async def _get_info_source(info_source_id: str):
        result = await db_session.execute(
            select(InfoSource).where(InfoSource.info_source_id == info_source_id)
        )
        source = result.scalars().first()
        if source is None:
            raise NotFound(f"info_source {info_source_id} not found")
        out = MagicMock()
        out.info_source_id = str(source.info_source_id)
        out.url = source.url
        out.source_specs = source.source_specs or []
        # Bridge: synthesize source_spec for fetch_info_item_bindings compat
        # (fetch_info_item_bindings is deleted in Phase A step 3).
        spec = MagicMock()
        spec.additional_properties = (source.source_specs or [{}])[0]
        out.source_spec = spec
        return out

    async def _get_info_item(info_item_id: str):
        result = await db_session.execute(
            select(InfoItem).where(InfoItem.info_item_id == info_item_id)
        )
        item = result.scalars().first()
        if item is None:
            raise NotFound(f"info_item {info_item_id} not found")
        bindings_result = await db_session.execute(
            select(InfoItemSource).where(
                InfoItemSource.info_item_id == item.info_item_id,
                InfoItemSource.deactivated_at.is_(None),
            )
        )
        out = MagicMock()
        out.info_item_id = str(item.info_item_id)
        out.name = item.name
        out.description = item.description
        info_item_sources = []
        for binding in bindings_result.scalars().all():
            b = MagicMock()
            b.info_source_id = str(binding.info_source_id)
            b.role = None  # v4.0.0: role removed; all active bindings are primary
            b.is_active = binding.deactivated_at is None
            b.deactivated_at = binding.deactivated_at
            info_item_sources.append(b)
        out.info_item_sources = info_item_sources
        return out

    async def _find_info_item(query: str, *, limit: int = 20):
        # ILIKE on name + description, mirroring Archiver's pg_trgm-backed
        # find_info_item, but minus the trigram ranking — substring is fine
        # for tests.
        q = f"%{query}%"
        result = await db_session.execute(
            select(InfoItem)
            .where(or_(InfoItem.name.ilike(q), InfoItem.description.ilike(q)))
            .order_by(InfoItem.created_at.desc())
            .limit(limit)
        )
        items = result.scalars().all()
        out = []
        for item in items:
            entry = MagicMock()
            entry.info_item_id = str(item.info_item_id)
            entry.name = item.name
            entry.description = item.description
            entry.created_at = item.created_at or datetime.now(UTC)
            entry.updated_at = item.updated_at or datetime.now(UTC)
            out.append(entry)
        return out

    fake_client.list_info_items = AsyncMock(side_effect=_list_info_items)
    fake_client.get_primary_info_spec = AsyncMock(side_effect=_get_primary_info_spec)
    fake_client.get_info_source = AsyncMock(side_effect=_get_info_source)
    fake_client.get_info_item = AsyncMock(side_effect=_get_info_item)
    fake_client.find_info_item = AsyncMock(side_effect=_find_info_item)

    # Swap the registry singleton via the test seam so
    # ``get_registry().get_archiver_client()`` returns this fake everywhere.
    # ``set_registry_for_testing(None)`` on teardown lets the next call rebuild
    # a fresh default — no leakage between tests.
    new_reg = ServiceRegistry(archiver_client=fake_client)
    set_registry_for_testing(new_reg)
    request.addfinalizer(lambda: set_registry_for_testing(None))
    return fake_client


@pytest.fixture
async def client(test_engine, db_session, info_client) -> AsyncGenerator[AsyncClient]:
    from src.api.main import app

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    async def override_probe_fn():
        return _make_mock_probe()

    async def override_dashboard_user():
        stmt = (
            pg_insert(AppUser)
            .values(id="test-user-id", email="test@example.com")
            .on_conflict_do_update(index_elements=["id"], set_={"email": "test@example.com"})
            .returning(AppUser)
        )
        result = await db_session.execute(stmt)
        return result.scalar_one()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_probe_fn] = override_probe_fn
    app.dependency_overrides[get_dashboard_user] = override_dashboard_user
    app.dependency_overrides[require_api_key] = lambda: "test-user-id"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
