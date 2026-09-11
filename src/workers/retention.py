"""Job-history retention: prune finished Procrastinate jobs, hourly (#296 D7).

``procrastinate_events`` and ``procrastinate_jobs`` were 96 % of the database —
679 k finished jobs back to March, three events each, growing ~5 k a day, and
nothing anywhere deleting them. The history is diagnostic, not data: nothing in
Watcher reads a finished job back.

The policy is two horizons. **Succeeded** jobs go after 7 days. **Failed,
cancelled and aborted** ones go after 30, because a failure is the thing someone
comes back to read. Unfinished jobs (``todo``, ``doing``) are live work and are
never touched: ``delete_old_jobs`` only considers final states. Events go with
their job (``ON DELETE CASCADE``), and ``watcher_app`` already holds ``DELETE``
on both tables, so this needs no grant.

Procrastinate's ``delete_old_jobs`` is one ``DELETE`` over a ``DISTINCT ON``
scan of every job joined to its events. That is trivial at steady state and a
sort of every event in the table on the original backlog, so the backlog is
pruned once by ``src/ops/prune_job_history.py``, which steps the horizon down
from the oldest job with :func:`backlog_horizons`, before this task ever runs
against it.
"""

from procrastinate import JobContext
from procrastinate.manager import JobManager

from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)

SUCCEEDED_RETENTION_HOURS = 7 * 24
FAILED_RETENTION_HOURS = 30 * 24


async def apply_retention(job_manager: JobManager, *, horizon_hours: int | None = None) -> None:
    """Delete finished jobs older than the policy, or than ``horizon_hours``.

    ``horizon_hours`` is for the backlog prune: the same two deletions at a
    longer age. It may never be *shorter* than the policy — the backlog prune
    can only ever be gentler than the task — so a horizon inside it is refused.
    """
    succeeded_hours = SUCCEEDED_RETENTION_HOURS if horizon_hours is None else horizon_hours
    if succeeded_hours < SUCCEEDED_RETENTION_HOURS:
        raise ValueError(
            f"horizon {succeeded_hours}h is inside the {SUCCEEDED_RETENTION_HOURS}h policy"
        )
    failed_hours = max(FAILED_RETENTION_HOURS, succeeded_hours)
    await job_manager.delete_old_jobs(nb_hours=succeeded_hours)
    await job_manager.delete_old_jobs(
        nb_hours=failed_hours,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )


def backlog_horizons(*, oldest_hours: int, step_hours: int) -> list[int]:
    """Horizons for pruning a backlog: from the oldest job down to the policy.

    Each step deletes one slice of history in its own statement, so no single
    transaction carries the whole backlog. Ends exactly on the policy.
    """
    if step_hours <= 0:
        raise ValueError(f"step must be positive, got {step_hours}h")
    horizons: list[int] = []
    horizon = oldest_hours
    while horizon > SUCCEEDED_RETENTION_HOURS:
        horizons.append(horizon)
        horizon -= step_hours
    horizons.append(SUCCEEDED_RETENTION_HOURS)
    return horizons


@bp.periodic(cron="23 * * * *", periodic_id="prune_job_history")
@bp.task(name="prune_job_history", queue="default", pass_context=True)
async def prune_job_history(context: JobContext, **periodic_kwargs) -> None:
    """Apply the retention policy. Hourly, off the minute the other ticks share.

    At steady state each run deletes about an hour's worth of history, so the
    statement stays small and the worker is not held for it. A failure raises:
    Procrastinate records the failed job, and the next hour is the retry.
    """
    await apply_retention(context.app.job_manager)
    logger.info(
        "job history pruned",
        extra={
            "succeeded_retention_hours": SUCCEEDED_RETENTION_HOURS,
            "failed_retention_hours": FAILED_RETENTION_HOURS,
        },
    )
