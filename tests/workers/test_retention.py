"""Tests for job-history retention (#296 D7).

``procrastinate_events`` and ``procrastinate_jobs`` were 96 % of the database —
679 k finished jobs back to March, with no retention anywhere. The policy is 7
days for succeeded jobs and 30 for failed, cancelled and aborted ones: failures
are what someone goes back to read.

Run against procrastinate's own ``InMemoryConnector`` through a real
``JobManager``, so the status mapping behind ``include_failed`` and friends is
procrastinate's, not a mock's.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from procrastinate.manager import JobManager
from procrastinate.testing import InMemoryConnector

from src.workers import get_app, reset_app, retention

HOUR = timedelta(hours=1)


def _seed(connector: InMemoryConnector, rows: dict[str, tuple[str, float]]) -> dict[int, str]:
    """Add one job per row, finished ``age_hours`` ago; return id → label."""
    now = datetime.now(UTC)
    labels: dict[int, str] = {}
    for job_id, (label, (status, age_hours)) in enumerate(rows.items(), start=1):
        connector.jobs[job_id] = {
            "id": job_id,
            "queue_name": "default",
            "task_name": "some_task",
            "status": status,
        }
        connector.events[job_id] = [
            {"type": "deferred", "at": now - (age_hours + 1) * HOUR},
            {"type": status, "at": now - age_hours * HOUR},
        ]
        labels[job_id] = label
    return labels


def _remaining(connector: InMemoryConnector, labels: dict[int, str]) -> set[str]:
    return {labels[job_id] for job_id in connector.jobs}


class TestApplyRetention:
    async def test_the_policy(self) -> None:
        connector = InMemoryConnector()
        labels = _seed(
            connector,
            {
                "succeeded, 1 day": ("succeeded", 24),
                "succeeded, 8 days": ("succeeded", 24 * 8),
                "failed, 20 days": ("failed", 24 * 20),
                "failed, 31 days": ("failed", 24 * 31),
                "cancelled, 31 days": ("cancelled", 24 * 31),
                "aborted, 31 days": ("aborted", 24 * 31),
            },
        )

        await retention.apply_retention(JobManager(connector))

        assert _remaining(connector, labels) == {"succeeded, 1 day", "failed, 20 days"}

    async def test_unfinished_jobs_are_never_touched(self) -> None:
        """Only final states are history. A job still ``todo`` or ``doing`` is
        live work, however old its events."""
        connector = InMemoryConnector()
        labels = _seed(connector, {"todo": ("todo", 24 * 90), "doing": ("doing", 24 * 90)})

        await retention.apply_retention(JobManager(connector))

        assert _remaining(connector, labels) == {"todo", "doing"}

    async def test_a_horizon_can_be_passed_for_the_backlog(self) -> None:
        """The operator prune steps the horizon down from the oldest job; each
        step is the same policy at a longer age, never a shorter one."""
        connector = InMemoryConnector()
        labels = _seed(
            connector,
            {
                "succeeded, 60 days": ("succeeded", 24 * 60),
                "succeeded, 40 days": ("succeeded", 24 * 40),
                "failed, 60 days": ("failed", 24 * 60),
            },
        )

        await retention.apply_retention(JobManager(connector), horizon_hours=24 * 50)

        assert _remaining(connector, labels) == {"succeeded, 40 days"}

    async def test_a_horizon_inside_the_policy_is_refused(self) -> None:
        """A horizon shorter than the policy would delete history the policy
        keeps; the backlog prune can only ever be gentler than the task."""
        with pytest.raises(ValueError):
            await retention.apply_retention(
                JobManager(InMemoryConnector()),
                horizon_hours=retention.SUCCEEDED_RETENTION_HOURS - 1,
            )


class TestBacklogHorizons:
    def test_steps_down_to_the_policy_and_ends_on_it(self) -> None:
        assert retention.backlog_horizons(oldest_hours=24 * 30, step_hours=24 * 7) == [
            24 * 30,
            24 * 23,
            24 * 16,
            24 * 9,
            retention.SUCCEEDED_RETENTION_HOURS,
        ]

    def test_nothing_older_than_the_policy_is_one_step(self) -> None:
        assert retention.backlog_horizons(oldest_hours=24, step_hours=24 * 7) == [
            retention.SUCCEEDED_RETENTION_HOURS
        ]

    def test_a_non_positive_step_is_refused(self) -> None:
        with pytest.raises(ValueError):
            retention.backlog_horizons(oldest_hours=24 * 30, step_hours=0)


class TestPruneJobHistoryTask:
    async def test_the_task_applies_the_policy_through_its_context(self) -> None:
        context = MagicMock()
        context.app.job_manager = JobManager(InMemoryConnector())
        apply = AsyncMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(retention, "apply_retention", apply)
            await retention.prune_job_history(context, timestamp=0)

        apply.assert_awaited_once_with(context.app.job_manager)

    def test_registered_as_an_hourly_periodic_task(self) -> None:
        reset_app()
        try:
            app = get_app()
            assert "prune_job_history" in app.tasks
            periodic = {name for name, _ in app.periodic_registry.periodic_tasks}
            assert "prune_job_history" in periodic
        finally:
            reset_app()
