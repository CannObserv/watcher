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

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.api.deps import get_db_session, get_probe_fn, require_api_key
from src.core.models import Base
from src.core.models.app_user import AppUser
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.probe import ProbeResult
from src.dashboard.deps import get_dashboard_user
from src.information.core.models.base import Base as InformationBase
from src.information.core.models.info_item import InfoItem  # noqa: F401  registers mapper
from src.information.core.models.info_spec import InfoSpec  # noqa: F401  registers mapper

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


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Information service owns its own DeclarativeBase + ``information`` schema.
        # Both must exist before tests run; mirror what alembic_information.ini
        # would create in production.
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS information"))
        await conn.run_sync(InformationBase.metadata.create_all)
        # ``Base.metadata`` carries a stub ``information.info_items`` table
        # (see ``src/core/models/watch.py``). Restrict ``create_all`` to
        # public-schema tables so we don't redefine InformationBase's table.
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
        # ``Base.metadata`` carries a stub ``information.info_items`` table
        # (see ``src/core/models/watch.py``) so the cross-schema FK on
        # ``watches.info_item_id`` resolves at import time. Drop only
        # public-schema tables here; the Information service owns
        # ``information.*`` and is dropped separately below.
        watcher_tables = [t for t in Base.metadata.sorted_tables if t.schema in (None, "public")]
        await conn.run_sync(Base.metadata.drop_all, tables=watcher_tables)
        await conn.run_sync(InformationBase.metadata.drop_all)
        await conn.execute(text("DROP SCHEMA IF EXISTS information CASCADE"))
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
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient]:
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
