"""Shared test fixtures — async database session and FastAPI TestClient.

tests/fixtures/ holds static sample files used by extractor tests (e.g. sample.html).

Factory contract (#185 Phase A)
--------------------------------
The module-level async helpers ``make_watched_item``, ``make_info_item``,
``make_info_source``, and ``bind_primary_source`` are NOT pytest fixtures —
they are awaitable factory functions test code can call directly.

``make_watched_item`` is the single WatchedItem factory (#191 collapse). It
takes an optional ``archiver_info_item_id``; when omitted, an InfoItem +
primary InfoSource + binding are auto-created to honour the 1:1
``watched_items.archiver_info_item_id`` uniqueness constraint, and the
InfoSource's id seeds ``archiver_info_source_id`` (both links are NOT NULL
since #251). The legacy ``target_info_source_id`` / ``schedule_config``
columns are gone.

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
from pathlib import Path
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select, text
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
# `test_engine` below runs create_all/drop_all and _apply_archiver_migrations
# subprocess-invokes Archiver's alembic. Both are migration-shaped work, and
# both are safe because the _test/_dev suffix check above already ran.
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
# /etc/watcher/.env carries NOTIFIER_BASE_URL and NOTIFIER_API_KEY, and
# AGENTS.md tells every agent to `source scripts/load-env.sh` before pytest. A
# stray database row is recoverable and a stray bus frame is inert; a stray
# notification is *delivered*, to real subscribers on real channels, and cannot
# be recalled. Worse, it succeeds — so unlike the two above it leaves no error
# behind to notice.
#
# Belt to the NOTIFIER_ENABLED gate's braces (src/core/notifier_client): the
# gate holds for every launch path, this makes "no notifier" the default for
# this one. Tests that want a client set both vars via monkeypatch.setenv,
# which restores itself on teardown.
#
# USE_REMOTE_NOTIFY is deliberately NOT cleared: nothing in src/ has read it
# since the local Apprise path was removed, and clearing it would advertise a
# switch that does not exist. See tests/test_notifier_isolation.py.
#
# At import, for the same reason as the four above, and never restored.
os.environ.pop("NOTIFIER_BASE_URL", None)
os.environ.pop("NOTIFIER_API_KEY", None)


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
    write ``information.*`` rows the WatchedItem factories reference. (Until
    #254 they also backed a fake ArchiverClient fixture; the SDK is gone, the
    tables are still real.) We invoke archiver's own alembic
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
# Tests call these directly:  ``wi = await make_watched_item(db_session, name="X")``
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

    An InfoItem + primary InfoSource + binding are auto-created when
    ``archiver_info_item_id`` is not supplied, so the WatchedItem references a
    real Archiver InfoItem and its InfoSource. Both links are NOT NULL (#251) —
    there is no bare-URL variant to construct.

    Extra ``**kwargs`` flow into the WatchedItem constructor — e.g.
    ``is_active``, ``content_media_type``, ``default_tags``, ``description``,
    ``default_schedule_config``, ``domain_suspended``, ``archived_at``.
    ``primary_url`` seeds ``effective_url`` (and the auto-created InfoSource URL).
    Pass ``domain_name=`` to set ``WatchedItem.domain_name`` (auto-creating the
    Domain row).
    """
    if archiver_info_item_id is None:
        item = await make_info_item(session)
        archiver_info_item_id = item.info_item_id
        primary = await make_info_source(session, url=primary_url)
        await bind_primary_source(
            session,
            info_item_id=archiver_info_item_id,
            info_source_id=primary.info_source_id,
        )
        if archiver_info_source_id is None:
            archiver_info_source_id = str(primary.info_source_id)
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
