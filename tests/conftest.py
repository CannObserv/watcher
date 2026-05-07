"""Shared test fixtures — async database session and FastAPI TestClient.

tests/fixtures/ holds static sample files used by extractor tests (e.g. sample.html).

Phase 2c migration shim
-----------------------
The module-level async helpers ``make_watch``, ``make_snapshot``, ``make_info_item``,
and ``make_info_spec`` are NOT pytest fixtures — they are awaitable factory
functions test code can call directly. ``default_snapshot_fixture`` and
``make_change`` remain as pytest fixtures for the handful of tests that still
consume them in fixture form.

The ``make_watch`` factory keeps a ``hasattr(Watch, "info_item_id")`` guard so
the helper does not silently re-introduce model coupling if the column is ever
renamed or scoped onto a different mapper.
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
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.api.deps import get_db_session, get_probe_fn, require_api_key
from src.core.models import Base
from src.core.models.app_user import AppUser
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.probe import ProbeResult
from src.core.registry import ServiceRegistry, set_registry_for_testing
from src.dashboard.deps import get_dashboard_user
from tests._information_test_models import (
    InfoItem,  # noqa: F401  registers mapper
    InfoSpec,  # noqa: F401  registers mapper
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
    repo (`/home/exedev/archiver`). Watcher tests need real `info_items` /
    `info_specs` tables so the cross-schema FK from `watches.info_item_id`
    resolves and seeded rows survive the FK check. We invoke archiver's own
    alembic instead of mirroring the schema in `tests/_information_test_models.py`
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
        # ``Base.metadata`` carries a stub ``information.info_items`` table
        # (see ``src/core/models/watch.py``) for cross-schema FK resolution.
        # Restrict ``create_all`` to public-schema tables — the `information`
        # schema is owned by Archiver's alembic above.
        watcher_tables = [t for t in Base.metadata.sorted_tables if t.schema in (None, "public")]
        await conn.run_sync(Base.metadata.create_all, tables=watcher_tables)
        # DB triggers are not part of the ORM model; recreate them here to mirror migrations.
        await conn.execute(
            text("""
            CREATE OR REPLACE FUNCTION trg_fn_watches_last_changed_at()
            RETURNS TRIGGER AS $$
            BEGIN
                UPDATE watches
                   SET last_changed_at = NEW.detected_at
                 WHERE id = NEW.watch_id;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        )
        await conn.execute(
            text("""
            CREATE OR REPLACE TRIGGER trg_changes_update_last_changed_at
            AFTER INSERT ON changes
            FOR EACH ROW
            EXECUTE FUNCTION trg_fn_watches_last_changed_at();
        """)
        )
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
    """Create and flush an InfoSpec row attached to *info_item*."""
    extraction = (
        {"algorithm": "css", "selector": selector} if selector else {"algorithm": "full_page"}
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
    """Construct a Watch with auto-created InfoItem + primary InfoSpec.

    Migration shim — handles three model states (Task 0 / 3 / 4) via
    ``hasattr`` guards on the Watch class.
    """
    if info_item_id is None:
        info_item = await make_info_item(session, name=name)
        await make_info_spec(
            session,
            info_item,
            url=url or "https://example.com",
            selector=selector,
        )
        info_item_id = info_item.info_item_id

    watch_kwargs = {"name": name, **kwargs}
    watch_kwargs.setdefault("content_type", content_type or ContentType.HTML)

    if hasattr(Watch, "info_item_id"):
        watch_kwargs["info_item_id"] = info_item_id

    watch = Watch(**watch_kwargs)
    session.add(watch)
    await session.flush()
    return watch


async def make_snapshot(session, watch, *, fetcher_used="http", **kwargs):
    """Create and flush a Snapshot attached to *watch*."""
    snapshot = Snapshot(watch_id=watch.id, fetcher_used=fetcher_used, **kwargs)
    session.add(snapshot)
    await session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# Legacy pytest-fixture variants (renamed to avoid name collision with the
# module-level helpers above). Tests that consume them as fixtures keep
# working: ``def test_x(default_watch_fixture)``.
# ---------------------------------------------------------------------------


@pytest.fixture
def default_snapshot_fixture(db_session):
    """Factory fixture: create and flush a Snapshot row attached to *watch* (legacy form)."""

    async def _make(watch, fetcher_used="http", **kwargs):
        snapshot = Snapshot(watch_id=watch.id, fetcher_used=fetcher_used, **kwargs)
        db_session.add(snapshot)
        await db_session.flush()
        return snapshot

    return _make


@pytest.fixture
def make_change(db_session):
    """Factory fixture: create and flush a Change row linking two snapshots."""

    async def _make(watch, current_snapshot, previous_snapshot=None, **kwargs):
        change = Change(
            watch_id=watch.id,
            current_snapshot_id=current_snapshot.id,
            previous_snapshot_id=previous_snapshot.id if previous_snapshot else None,
            **kwargs,
        )
        db_session.add(change)
        await db_session.flush()
        return change

    return _make


@pytest.fixture
def info_client(db_session, request):
    """Mock ArchiverClient backed by the test DB's ``information.*`` tables.

    Routes pull the SDK via ``get_registry().get_archiver_client()``.
    This fixture swaps the registry singleton's cached client for an
    AsyncMock whose ``list_info_items`` / ``get_primary_info_spec`` methods
    look up live rows in ``db_session`` so tests can seed an InfoItem +
    InfoSpec via ``make_info_item`` / ``make_info_spec`` and have routes
    behave exactly as they would against the real Information service.

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
        result = await db_session.execute(
            select(InfoSpec)
            .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
            .order_by(InfoSpec.priority.asc())
        )
        spec = result.scalars().first()
        if spec is None:
            raise NotFound(f"no active spec for info_item {info_item_id}")
        out = MagicMock()
        out.info_item_id = str(spec.info_item_id)
        out.info_spec_id = str(spec.info_spec_id)
        doc = MagicMock()
        doc.to_dict = MagicMock(return_value=dict(spec.document))
        out.document = doc
        return out

    fake_client.list_info_items = AsyncMock(side_effect=_list_info_items)
    fake_client.get_primary_info_spec = AsyncMock(side_effect=_get_primary_info_spec)

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
