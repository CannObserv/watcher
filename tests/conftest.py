"""Shared test fixtures — async database session and FastAPI TestClient.

tests/fixtures/ holds static sample files used by extractor tests (e.g. sample.html).

Factory contract (#185 Phase A)
--------------------------------
``make_watched_item`` is a module-level async helper, NOT a pytest fixture —
test code awaits it directly.

It is the single WatchedItem factory (#191 collapse). Both Archiver links —
``archiver_info_item_id`` and ``archiver_info_source_id`` — are NOT NULL (#251)
and default to fresh ULIDs. They name rows in Archiver's own database with no
foreign key behind them, so nothing has to exist at the other end; pass
``archiver_info_item_id=`` when a test needs the id up front (the column is
unique — one WatchedItem per InfoItem).

#311 removed the ``information`` schema, its test-only mappers, and the
``make_info_item`` / ``make_info_source`` / ``bind_primary_source`` factories.
They existed only to mint those two ULIDs, and did it by subprocess-running the
sibling Archiver checkout's alembic to build a schema production does not have
(#271). ``tests/test_archiver_isolation.py`` keeps it gone.

Phase 5 (#156): ``make_snapshot`` and ``default_snapshot_fixture`` removed —
Snapshot table dropped. ``InfoSpec`` table and ``make_info_spec`` factory
also dead-code-removed under #160.
"""

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from ulid import ULID

from src.api.deps import get_db_session, get_probe_fn, require_api_key
from src.core import db_safety
from src.core.database import MIGRATION_DATABASE_URL_ENV
from src.core.models import Base
from src.core.models.app_user import AppUser
from src.core.models.domain import Domain
from src.core.models.watched_item import WatchedItem
from src.core.probe import ProbeResult
from src.dashboard.deps import get_dashboard_user

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL environment variable is not set. Load env: source scripts/load-env.sh"
    )

if not db_safety.is_non_production_database(TEST_DATABASE_URL):
    raise RuntimeError(
        f"TEST_DATABASE_URL points at database "
        f"{db_safety.database_name(TEST_DATABASE_URL)!r}, whose name carries no "
        "_test/_dev suffix — refusing to run: teardown creates and drops tables "
        "and would destroy production data. Point it at a dedicated test "
        "database, e.g. watcher_test. (#233)"
    )

# Point the application itself at the test database for the whole session.
#
# Anything resolving the URL from the environment rather than through the
# `get_db_session` override — src.core.database.get_engine(), alembic's
# get_url(), the src.workers connector, and the src.core.db_safety production
# guard — would otherwise read the *production* DATABASE_URL that
# /etc/watcher/.env supplies. Pinning it makes the override and the
# environment agree, and means a test can never reach production even if it
# bypasses the dependency override.
#
# PROCRASTINATE_DATABASE_URL is cleared because src.workers consults it
# before DATABASE_URL.
#
# At import rather than in a fixture because it must be in place before *any*
# fixture resolves the URL, and deliberately never restored — the process
# exists only to run this suite, and a restore would hand the production URL
# back. This is the single mechanism managing these two variables. (#233)
#
# WATCHER_MIGRATION_DATABASE_URL is pinned for the same reason and is the
# sharper edge of it (#259): it is the credential that holds DDL rights, so an
# inherited production value would let anything invoking alembic migrate
# production even with DATABASE_URL pointed here. Pinned rather than cleared —
# clearing falls back to DATABASE_URL, which is right only until a test sets it.
# The suite does hold DDL rights on the database it names, deliberately:
# `test_engine` below runs create_all/drop_all and drops any leftover
# `information` schema (#311). Both are migration-shaped work, and both are
# safe because the _test/_dev suffix check above already ran.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ[MIGRATION_DATABASE_URL_ENV] = TEST_DATABASE_URL
os.environ.pop("PROCRASTINATE_DATABASE_URL", None)

# The same hazard in bus form, and not hypothetical: a test run under an
# exported /etc/watcher/.env published a fabricated `source_revision_observed`
# frame onto the *production* `content.revisions` stream. It was inert —
# Archiver's consumer dropped it as an unknown info_source — but the producer
# reached production Redis from a test, which is the #233 failure with a
# different variable.
#
# Any producer resolving the client from the environment rather than through an
# injected one (`get_shared_bus_client`, `bus_client_from_env`) reads whatever
# WATCHER_BUS_REDIS_URL supplies. Clearing it makes "no bus" the test default,
# so a test that forgets to inject a fakeredis client publishes nowhere instead
# of onto the live stream. Tests that want a bus pass one explicitly.
#
# At import, for the same reason as the two above, and never restored.
os.environ.pop("WATCHER_BUS_REDIS_URL", None)
os.environ.pop("WATCHER_DEV_BUS_REDIS_URL", None)

# The same hazard again, and the one with the largest blast radius (#277):
# /etc/watcher/.env carried WATCHER_NOTIFIER_BASE_URL and WATCHER_NOTIFIER_API_KEY
# until #278 moved them to a unit-only file, and AGENTS.md tells every agent to
# `source scripts/load-env.sh` before pytest. A stray database row is
# recoverable and a stray bus frame is inert; a stray notification is
# *delivered*, to real subscribers on real channels, and cannot be recalled.
# Worse, it succeeds — so unlike the two above it leaves no error behind to
# notice.
#
# Kept after the move, and not redundantly: the scrub is what makes this suite
# indifferent to where the credential lives. A developer who exports the pair by
# hand, a future env file that reacquires it, a `.env` holding a scratch
# notifier — all of them stop here, and the guarantee this module owns ("no test
# can dispatch") stays a property of the suite rather than of the VM's file
# layout. Tests that want a client set both vars via monkeypatch.setenv, which
# restores itself on teardown.
#
# WATCHER_NOTIFIER_ENABLED joins them under #278, which made the flag-without-a-URL
# combination a startup failure in its own right: an exported flag would
# otherwise turn every lifespan test into an environment failure.
#
# The WATCHER_DEV_NOTIFIER_* pair goes too (CR-3), matching the dev variable the
# bus block above already pops. Nothing in src/ reads either — they are
# scripts/dev_server.sh's — but they live in the repo .env that load-env.sh
# exports before every pytest run, and #278 asked notifier for a development
# key that belongs in exactly that file. All five cleared means the session's
# answer to "is there a notifier?" is a flat no, whatever the launching shell
# was holding.
#
# USE_REMOTE_NOTIFY is deliberately NOT cleared: nothing in src/ has read it
# since the local Apprise path was removed, and clearing it would advertise a
# switch that does not exist. See tests/test_notifier_isolation.py.
#
# At import, for the same reason as the four above, and never restored.
os.environ.pop("WATCHER_NOTIFIER_BASE_URL", None)
os.environ.pop("WATCHER_NOTIFIER_API_KEY", None)
os.environ.pop("WATCHER_NOTIFIER_ENABLED", None)
os.environ.pop("WATCHER_DEV_NOTIFIER_BASE_URL", None)
os.environ.pop("WATCHER_DEV_NOTIFIER_API_KEY", None)

# The dashboard's public base URL (#296 D6) lives in /etc/watcher/.env, so a
# suite launched from a loaded shell would inherit production's host and every
# expected link would silently depend on which VM ran it. Unset is "no link";
# tests that render one set a host of their own.
os.environ.pop("WATCHER_PUBLIC_BASE_URL", None)
os.environ.pop("WATCHER_DEV_PUBLIC_BASE_URL", None)


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
        # Production carries no `information` schema (#271). Until #311 this
        # fixture built one from the sibling Archiver checkout's alembic and
        # left it alive between sessions (#150), so a test database that ran
        # the old suite still holds it: drop it, so the schema here is the
        # schema production has. Safe — the _test/_dev suffix check ran at import.
        await conn.execute(text("DROP SCHEMA IF EXISTS information CASCADE"))
        await conn.run_sync(Base.metadata.create_all)
        # Phase 5 (#156): trg_changes_update_last_changed_at trigger removed.
        # No triggers to recreate.
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


# ---------------------------------------------------------------------------
# Module-level async factory (NOT a pytest fixture).
#
# Tests call it directly:  ``wi = await make_watched_item(db_session, name="X")``
# ---------------------------------------------------------------------------


async def make_watched_item(
    session,
    *,
    name="Test Watched Item",
    archiver_info_item_id=None,
    archiver_info_source_id=None,
    primary_url="https://example.com",
    domain_name=None,
    **kwargs,
):
    """Construct a WatchedItem — the single monitored entity (#191 collapse).

    Both Archiver links are NOT NULL (#251) — there is no bare-URL variant to
    construct — and each defaults to a fresh ULID when not supplied. They name
    rows in Archiver's own database with no foreign key behind them, so nothing
    is created at the other end (#311).

    Extra ``**kwargs`` flow into the WatchedItem constructor — e.g.
    ``is_active``, ``content_media_type``, ``default_tags``, ``description``,
    ``default_schedule_config``, ``domain_suspended``, ``archived_at``.
    ``primary_url`` seeds ``effective_url``.
    Pass ``domain_name=`` to set ``WatchedItem.domain_name`` (auto-creating the
    Domain row).
    """
    if archiver_info_item_id is None:
        archiver_info_item_id = ULID()
    if archiver_info_source_id is None:
        archiver_info_source_id = str(ULID())

    # Auto-create the Domain row first if a domain_name is requested (FK).
    if domain_name is not None:
        existing_domain = (
            await session.execute(select(Domain).where(Domain.name == domain_name))
        ).scalar_one_or_none()
        if existing_domain is None:
            session.add(Domain(name=domain_name))
            await session.flush()

    wi = WatchedItem(
        archiver_info_item_id=archiver_info_item_id,
        archiver_info_source_id=archiver_info_source_id,
        name=name,
        effective_url=primary_url,
        domain_name=domain_name,
        **kwargs,
    )
    session.add(wi)
    await session.flush()
    return wi


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
