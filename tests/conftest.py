"""Shared test fixtures — async database session and FastAPI TestClient."""

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.api.dependencies import get_db_session, get_probe_fn
from src.core.models import Base
from src.core.models.app_user import AppUser
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import Watch
from src.core.probe import ProbeResult
from src.dashboard.deps import get_dashboard_user

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
        await conn.run_sync(Base.metadata.create_all)
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
        await conn.run_sync(Base.metadata.drop_all)
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


@pytest.fixture
def make_watch(db_session):
    """Factory fixture: create and flush a Watch row."""

    async def _make(name="Test Watch", url="https://example.com", content_type="html", **kwargs):
        watch = Watch(name=name, url=url, content_type=content_type, **kwargs)
        db_session.add(watch)
        await db_session.flush()
        return watch

    return _make


@pytest.fixture
def make_snapshot(db_session):
    """Factory fixture: create and flush a Snapshot row attached to *watch*."""

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
        return AppUser(id="test-user-id", email="test@example.com")

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_probe_fn] = override_probe_fn
    app.dependency_overrides[get_dashboard_user] = override_dashboard_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
