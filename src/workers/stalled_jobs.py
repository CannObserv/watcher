"""Recover jobs a dead worker left in ``doing`` (#334).

A graceful stop is safe: procrastinate lets running jobs finish, aborts any
still running at ``SHUTDOWN_GRACEFUL_SECONDS`` (``src.workers.supervisor``),
and unregisters. A SIGKILL is not — ``TimeoutStopSec`` running out, an OOM
kill, a host crash — and neither is #340's in-process restart while the
cluster is down, since the dying worker can neither record its jobs' outcome
nor unregister. The job stays ``doing``; the next boot prunes the dead worker,
and ``worker_id`` goes NULL (``ON DELETE SET NULL``). Nothing else ever looks
at it again: job 62130 sat there 131 days.

Every five minutes this asks procrastinate for the ``doing`` jobs that have no
worker, or whose worker's heartbeat is older than :data:`STALLED_AFTER_SECONDS`,
and retries them — procrastinate's own recipe. Every task here is idempotent
(the apply path's status guard, the outbox drains, the publishers), so a re-run
is safe. The window is not procrastinate's default 30 s: the sweep runs inside
the only live worker, and ``JobContext`` carries no worker id to exclude that
worker's own jobs by. At procrastinate's default concurrency of 1 the one such
job is the sweep itself, and a heartbeat write that lags must not have it retry
itself — a bumped ``attempts`` and a false "stalled job retried".

Each job is recovered on its own: one that raises is logged and left ``doing``
for the next tick. The one known cause is a ``queueing_lock`` conflict —
procrastinate's unique index covers ``todo`` rows only, so an orphaned
``publish_watch_status`` cannot rejoin the queue while a coalesced republish
waits there, and by the next tick that republish has run.

A job that reaches the sweep with :data:`FAIL_AT_ATTEMPTS` attempts is failed
instead: one that is itself the cause of the kill — an OOM on one huge blob —
would otherwise loop retry → kill → restart for ever. Failed rows are kept 30
days (``src.workers.retention``) and counted on the dashboard. The count is
procrastinate's ``attempts``, which a task's own retries raise too, so a job
retried twice already is failed at its first orphaning; for each task that
retries, something else re-defers the work — ``reap_fetch_commands`` the
fetch applies, ``reap_process_commands`` ``apply_process_fact`` (which, when the
processor decides, is what closes the check — #326), ``schedule_tick`` a
``check_watched_item`` whose item is still due.
So is a job whose abort was requested before its worker died:
``procrastinate_retry_job_v2`` fails such a job rather than requeue it, so
failing it here keeps the log and the count true to the row.
"""

from procrastinate import JobContext
from procrastinate.jobs import Status
from procrastinate.manager import JobManager

from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)

#: A worker silent this long is dead. Procrastinate's heartbeat is every 10 s.
STALLED_AFTER_SECONDS = 120
#: A stalled job with this many attempts behind it is failed, not retried.
FAIL_AT_ATTEMPTS = 2

_FAILED_BECAUSE = {
    "attempts_cap": "stalled job failed: its worker died with it running, at the attempts cap",
    "abort_requested": "stalled job failed: its worker died before honouring a requested abort",
}


async def recover_stalled(job_manager: JobManager) -> dict[str, int]:
    """Retry each stalled job, or fail it (attempts cap, requested abort); return the counts."""
    retried = failed = 0
    stalled = list(
        await job_manager.get_stalled_jobs(seconds_since_heartbeat=STALLED_AFTER_SECONDS)
    )
    for job in stalled:
        extra = {
            "job_id": job.id,
            "task_name": job.task_name,
            "worker_id": job.worker_id,
            "attempts": job.attempts,
        }
        if job.abort_requested:
            reason = "abort_requested"
        elif job.attempts >= FAIL_AT_ATTEMPTS:
            reason = "attempts_cap"
        else:
            reason = None
        try:
            if reason:
                await job_manager.finish_job(job, status=Status.FAILED, delete_job=False)
            else:
                await job_manager.retry_job(job)
        except Exception:
            logger.error(
                "stalled job not recovered; the next sweep tries again",
                extra={**extra, "action": "none"},
                exc_info=True,
            )
            continue
        if reason:
            failed += 1
            logger.error(
                _FAILED_BECAUSE[reason], extra={**extra, "action": "failed", "reason": reason}
            )
        else:
            retried += 1
            logger.warning(
                "stalled job retried: its worker died with it running",
                extra={**extra, "action": "retried"},
            )
    return {"retried": retried, "failed": failed}


@bp.periodic(cron="*/5 * * * *", periodic_id="recover_stalled_jobs")
@bp.task(name="recover_stalled_jobs", queue="default", pass_context=True)
async def recover_stalled_jobs(context: JobContext, **periodic_kwargs) -> dict[str, int]:
    """Sweep for stalled jobs. A failure raises; the next tick is the retry."""
    return await recover_stalled(context.app.job_manager)
