"""Dashboard-scoped fixtures shared across dashboard test modules."""

# Phase 5 (#156): make_change_with_snapshots removed — Change/Snapshot tables dropped.

from collections.abc import AsyncGenerator
from datetime import datetime

import asyncpg
import pytest
from procrastinate.schema import SchemaManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import TEST_DATABASE_URL

# Everything Procrastinate's schema creates, in dependency order: the functions
# reference the types, the triggers go with their tables. Run before applying
# the schema as well as after, so a session killed mid-run doesn't wedge the
# next one. Scoped to the current schema and the ``procrastinate_`` prefix —
# the array types (``_procrastinate_…``) go with their element type.
_DROP_PROCRASTINATE_SCHEMA = r"""
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT p.oid::regprocedure AS ident FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate\_%'
    LOOP
        EXECUTE 'DROP FUNCTION IF EXISTS ' || obj.ident || ' CASCADE';
    END LOOP;
    FOR obj IN
        SELECT c.oid::regclass AS ident FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind = 'r'
          AND c.relname LIKE 'procrastinate\_%'
    LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || obj.ident || ' CASCADE';
    END LOOP;
    FOR obj IN
        SELECT t.oid::regtype AS ident FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = current_schema() AND t.typname LIKE 'procrastinate\_%'
    LOOP
        EXECUTE 'DROP TYPE IF EXISTS ' || obj.ident || ' CASCADE';
    END LOOP;
END $$;
"""


async def _run_script(sql: str) -> None:
    """Run a multi-statement script on its own connection.

    Procrastinate ships its schema as one script, and asyncpg only takes
    several statements at once through the simple protocol its ``execute``
    uses — SQLAlchemy drives the extended one, a statement at a time.
    """
    connection = await asyncpg.connect(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


@pytest.fixture(scope="session")
async def _procrastinate_schema() -> AsyncGenerator[None]:
    """Build Procrastinate's own tables in the test database, once (#298).

    ``Base.metadata.create_all`` never makes them — Procrastinate owns its
    schema and applies it with its own CLI — so ``get_queue_health`` otherwise
    hits ``ProgrammingError`` here and returns zeros. A test reading those
    zeros passes against any query at all, which is how a queue tile that
    counted almost nothing went unnoticed. Applying the shipped ``schema.sql``
    also makes drift impossible, the same reasoning as running Archiver's own
    alembic for the ``information`` schema (tests/conftest.py).

    Session-scoped and committed, not built inside the test transaction:
    rolling the enums back per test would leave asyncpg holding prepared
    statements whose type OIDs no longer exist. Per-test isolation is
    ``db_session``'s rollback, which still covers every row seeded below.
    """
    await _run_script(_DROP_PROCRASTINATE_SCHEMA)
    await _run_script(SchemaManager.get_schema())
    yield
    await _run_script(_DROP_PROCRASTINATE_SCHEMA)


@pytest.fixture
async def procrastinate_session(_procrastinate_schema, db_session: AsyncSession) -> AsyncSession:
    """``db_session``, with Procrastinate's tables present to seed and query."""
    return db_session


# ---------------------------------------------------------------------------
# Module-level async factories for Procrastinate rows (NOT pytest fixtures).
#
# Each drives Procrastinate's own SQL functions rather than writing the tables
# directly, so what the queue-health tests read is what the queue really
# produces: ``scheduled_at`` left NULL or set by the same code that sets it in
# production, and ``procrastinate_events`` rows written by its triggers.
# ---------------------------------------------------------------------------


async def defer_job(
    session: AsyncSession, *, task_name: str = "check_watched_item", queue: str = "default"
) -> int:
    """Defer a job the way ``defer_async`` does — no ``schedule_at``, so NULL."""
    return await session.scalar(
        text(
            "SELECT unnest(procrastinate_defer_jobs_v1(ARRAY[ROW("
            "CAST(:queue AS varchar), CAST(:task_name AS varchar), 0, NULL, NULL,"
            "CAST('{}' AS jsonb), CAST(NULL AS timestamptz)"
            ")]::procrastinate_job_to_defer_v1[]))"
        ),
        {"queue": queue, "task_name": task_name},
    )


async def defer_periodic_job(
    session: AsyncSession,
    *,
    task_name: str = "check_due_watched_items",
    periodic_id: str = "",
    defer_timestamp: int = 1,
    queue: str = "default",
) -> int:
    """Defer a job on a cron tick — the path that fills watcher's queue.

    ``procrastinate_defer_periodic_job_v2`` passes ``NULL::timestamptz`` for
    ``scheduled_at`` too, so a periodic job is as invisible to a
    ``scheduled_at`` filter as a plain one (#298).
    """
    return await session.scalar(
        text(
            "SELECT procrastinate_defer_periodic_job_v2("
            "CAST(:queue AS varchar), NULL, NULL, CAST(:task_name AS varchar), 0,"
            "CAST(:periodic_id AS varchar), :defer_timestamp, CAST('{}' AS jsonb))"
        ),
        {
            "queue": queue,
            "task_name": task_name,
            "periodic_id": periodic_id,
            "defer_timestamp": defer_timestamp,
        },
    )


async def start_job(session: AsyncSession, job_id: int) -> None:
    """Take a job todo → doing, the transition ``fetch_job`` makes."""
    await session.execute(
        text("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = :job_id"),
        {"job_id": job_id},
    )


async def finish_job(session: AsyncSession, job_id: int, *, status: str = "succeeded") -> None:
    """End a running job; the status trigger writes its terminal event."""
    await session.execute(
        text(
            "SELECT procrastinate_finish_job_v1("
            ":job_id, CAST(:status AS procrastinate_job_status), false)"
        ),
        {"job_id": job_id, "status": status},
    )


async def retry_job(session: AsyncSession, job_id: int, *, retry_at: datetime) -> None:
    """Re-queue a running job for a retry — the one path that sets ``scheduled_at``."""
    await session.execute(
        text("SELECT procrastinate_retry_job_v2(:job_id, :retry_at, NULL, NULL, NULL)"),
        {"job_id": job_id, "retry_at": retry_at},
    )


async def run_job_to_success(
    session: AsyncSession, *, task_name: str = "check_watched_item"
) -> int:
    """Defer, run and succeed a job on its first try. Returns its id."""
    job_id = await defer_job(session, task_name=task_name)
    await start_job(session, job_id)
    await finish_job(session, job_id)
    return job_id


async def backdate_job_events(session: AsyncSession, job_id: int, *, at: datetime) -> None:
    """Move a job's events back in time — nothing stamps ``at`` but the default."""
    await session.execute(
        text("UPDATE procrastinate_events SET at = :at WHERE job_id = :job_id"),
        {"at": at, "job_id": job_id},
    )
