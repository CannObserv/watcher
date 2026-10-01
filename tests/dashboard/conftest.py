"""Dashboard-scoped fixtures shared across dashboard test modules."""

# Phase 5 (#156): make_change_with_snapshots removed — Change/Snapshot tables dropped.

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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


async def run_job_to_success(session: AsyncSession) -> int:
    """Defer, run and succeed a job on its first try. Returns its id."""
    job_id = await defer_job(session)
    await start_job(session, job_id)
    await finish_job(session, job_id)
    return job_id


async def backdate_job_events(session: AsyncSession, job_id: int, *, at: datetime) -> None:
    """Move a job's events back in time — nothing stamps ``at`` but the default."""
    await session.execute(
        text("UPDATE procrastinate_events SET at = :at WHERE job_id = :job_id"),
        {"at": at, "job_id": job_id},
    )
