"""Tests for the stalled-job sweep (#334).

A worker that is SIGKILLed mid-job leaves the job in ``doing``. The next boot
prunes the dead worker row and ``procrastinate_jobs.worker_id`` is ``ON DELETE
SET NULL``, so the job sat in ``doing`` with no worker — job 62130 for 131 days.

The unit tests run on procrastinate's ``InMemoryConnector`` through a real
``App`` and ``JobManager``: jobs are deferred, fetched and orphaned the way the
worker does it. The one integration test runs the same path on procrastinate's
real SQL, because the fix rests on two facts of that SQL — the foreign key and
the ``worker_id IS NULL`` branch of the heartbeat query — that the in-memory
connector only imitates.
"""

import logging
from datetime import timedelta
from unittest.mock import MagicMock

import procrastinate
import pytest
from procrastinate import exceptions, utils
from procrastinate.testing import InMemoryConnector

from src.workers import get_app, get_conninfo, reset_app, stalled_jobs
from tests.conftest import TEST_DATABASE_URL


class _RetryConflicts(InMemoryConnector):
    """``procrastinate_jobs_queueing_lock_idx_v1`` covers ``todo`` rows only, so
    retrying an orphaned ``publish_watch_status`` while a coalesced republish
    waits in ``todo`` raises — as here, for the job ids listed."""

    def __init__(self) -> None:
        super().__init__()
        self.conflicting: set[int] = set()

    async def retry_job_run(self, job_id: int, **kwargs) -> None:
        if job_id in self.conflicting:
            raise exceptions.UniqueViolation(
                constraint_name="procrastinate_jobs_queueing_lock_idx_v1",
                queueing_lock="publish_watch_status",
            )
        await super().retry_job_run(job_id, **kwargs)


def _app(connector=None) -> procrastinate.App:
    app = procrastinate.App(connector=connector or InMemoryConnector())

    @app.task(name="some_task", queue="default")
    async def some_task() -> None:
        return None

    return app


async def _fetch(app: procrastinate.App) -> tuple[int, int]:
    """Start the next job on a fresh worker; return (job id, worker id)."""
    worker_id = await app.job_manager.register_worker()
    job = await app.job_manager.fetch_job(queues=None, worker_id=worker_id)
    assert job is not None and job.id is not None
    return job.id, worker_id


async def _start(app: procrastinate.App) -> tuple[int, int]:
    """Defer a job and start it on a fresh worker; return (job id, worker id)."""
    job_id = await app.tasks["some_task"].defer_async()
    started, worker_id = await _fetch(app)
    assert started == job_id
    return job_id, worker_id


def _heartbeat_age(app: procrastinate.App, worker_id: int, seconds: float) -> None:
    app.connector.workers[worker_id] = utils.utcnow() - timedelta(seconds=seconds)


def _job(app: procrastinate.App, job_id: int) -> dict:
    return app.connector.jobs[job_id]


class TestRecoverStalled:
    async def test_a_job_whose_worker_was_pruned_is_retried(self) -> None:
        """The 62130 shape: the next boot pruned the dead worker, so the job is
        ``doing`` with ``worker_id`` NULL."""
        app = _app()
        job_id, _ = await _start(app)
        await app.job_manager.prune_stalled_workers(0)
        assert _job(app, job_id)["worker_id"] is None

        result = await stalled_jobs.recover_stalled(app.job_manager)

        assert _job(app, job_id)["status"] == "todo"
        assert _job(app, job_id)["attempts"] == 1
        assert result == {"retried": 1, "failed": 0}

    async def test_a_job_whose_dead_worker_is_still_registered_is_retried(self) -> None:
        """A worker that died without unregistering — an OOM kill before the
        next boot's prune, or #340's in-process restart after a failed
        unregister — keeps its row, and only its heartbeat says it is gone."""
        app = _app()
        job_id, worker_id = await _start(app)
        _heartbeat_age(app, worker_id, 600)

        await stalled_jobs.recover_stalled(app.job_manager)

        assert _job(app, job_id)["status"] == "todo"

    async def test_a_job_under_a_live_worker_is_left_alone(self) -> None:
        app = _app()
        job_id, _ = await _start(app)

        result = await stalled_jobs.recover_stalled(app.job_manager)

        assert _job(app, job_id)["status"] == "doing"
        assert result == {"retried": 0, "failed": 0}

    async def test_a_heartbeat_a_minute_late_is_not_yet_stalled(self) -> None:
        """Procrastinate's default window is 30 s. The sweep runs inside the only
        live worker, and ``JobContext`` carries no worker id to exclude its own
        jobs by — so a heartbeat write that lags past 30 s must not hand the
        sweep a job that is still running beside it."""
        app = _app()
        job_id, worker_id = await _start(app)
        _heartbeat_age(app, worker_id, 60)

        await stalled_jobs.recover_stalled(app.job_manager)

        assert _job(app, job_id)["status"] == "doing"

    async def test_a_job_orphaned_three_times_is_failed_not_retried(self) -> None:
        """A job that is itself the cause of the kill — an OOM on one huge blob —
        would otherwise loop retry → kill → restart for ever."""
        app = _app()
        job_id = await app.tasks["some_task"].defer_async()
        statuses = []
        for _ in range(3):
            assert (await _fetch(app))[0] == job_id
            await app.job_manager.prune_stalled_workers(0)
            await stalled_jobs.recover_stalled(app.job_manager)
            statuses.append(_job(app, job_id)["status"])

        assert statuses == ["todo", "todo", "failed"]

    async def test_each_recovery_is_logged_with_the_job(self, caplog) -> None:
        app = _app()
        retried_id, _ = await _start(app)
        await app.job_manager.prune_stalled_workers(0)
        failed_id, _ = await _start(app)
        app.connector.jobs[failed_id]["attempts"] = 2
        await app.job_manager.prune_stalled_workers(0)

        with caplog.at_level(logging.WARNING, logger="src.workers.stalled_jobs"):
            await stalled_jobs.recover_stalled(app.job_manager)

        by_job = {r.job_id: r for r in caplog.records if r.name == "src.workers.stalled_jobs"}
        assert by_job[retried_id].levelno == logging.WARNING
        assert by_job[retried_id].task_name == "some_task"
        assert by_job[retried_id].action == "retried"
        assert by_job[failed_id].levelno == logging.ERROR
        assert by_job[failed_id].action == "failed"

    async def test_one_job_that_cannot_be_recovered_does_not_stop_the_rest(self, caplog) -> None:
        """Left as it is, the conflicting job is the next tick's — by then the
        queued republish has run and released the lock."""
        connector = _RetryConflicts()
        app = _app(connector)
        conflicting, _ = await _start(app)
        other, _ = await _start(app)
        await app.job_manager.prune_stalled_workers(0)
        connector.conflicting.add(conflicting)

        with caplog.at_level(logging.ERROR, logger="src.workers.stalled_jobs"):
            result = await stalled_jobs.recover_stalled(app.job_manager)

        assert _job(app, conflicting)["status"] == "doing"
        assert _job(app, other)["status"] == "todo"
        assert result == {"retried": 1, "failed": 0}
        (record,) = [r for r in caplog.records if r.name == "src.workers.stalled_jobs"]
        assert record.job_id == conflicting
        assert isinstance(record.exc_info[1], exceptions.UniqueViolation)


class TestRecoverStalledJobsTask:
    async def test_the_task_sweeps_through_its_context(self) -> None:
        app = _app()
        job_id, _ = await _start(app)
        await app.job_manager.prune_stalled_workers(0)
        context = MagicMock()
        context.app = app

        await stalled_jobs.recover_stalled_jobs(context, timestamp=0)

        assert _job(app, job_id)["status"] == "todo"

    def test_registered_as_a_periodic_task(self) -> None:
        """Every five minutes, under a stable periodic id — which is what
        de-duplicates a tick across restarts."""
        reset_app()
        try:
            app = get_app()
            periodic = app.periodic_registry.periodic_tasks
            task = periodic[("recover_stalled_jobs", "recover_stalled_jobs")]
            assert task.cron == "*/5 * * * *"
        finally:
            reset_app()


@pytest.mark.integration
async def test_procrastinates_own_sql_orphans_and_recovers_the_job(_procrastinate_schema) -> None:
    """Characterizes procrastinate 3.7.2's SQL: pruning a worker NULLs its jobs'
    ``worker_id`` (the foreign key), the heartbeat query still returns them, and
    ``procrastinate_retry_job_v2`` takes a ``doing`` job back to ``todo``."""
    app = _app(
        procrastinate.PsycopgConnector(conninfo=get_conninfo({"DATABASE_URL": TEST_DATABASE_URL}))
    )
    async with app.open_async():
        job_id = None
        try:
            job_id, _ = await _start(app)
            await app.job_manager.prune_stalled_workers(0)
            (orphan,) = await app.job_manager.list_jobs_async(id=job_id)
            assert orphan.status == "doing" and orphan.worker_id is None

            await stalled_jobs.recover_stalled(app.job_manager)

            (job,) = await app.job_manager.list_jobs_async(id=job_id)
            assert job.status == "todo"
            assert job.attempts == 1
        finally:
            if job_id is not None:
                await app.connector.execute_query_async(
                    "DELETE FROM procrastinate_jobs WHERE id = %(id)s", id=job_id
                )
