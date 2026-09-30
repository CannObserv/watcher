"""Supervisor for the embedded Procrastinate worker (#340).

A Postgres cluster restart kills procrastinate's LISTEN connection, and the
reconnect in ``PsycopgConnector.listen_notify`` has no retry (unchanged through
3.10.0): the ``listener`` side task fails, ``Worker._monitor_side_tasks`` calls
``stop()``, and ``run_worker_async`` returns *normally*. A worker that cannot
register at boot (no procrastinate schema, #341) raises instead. Either way the
process keeps serving with every periodic task stopped, so the lifespan runs the
worker through this loop rather than a bare task.

Restarted in-process rather than by exiting for ``Restart=on-failure``: an exit
drops in-flight API requests and consumer work to recover a component that
comes back on its own once the cluster does — the pool rides out the outage,
only the listener gives up.
"""

import asyncio
from typing import Protocol

from src.core.logging import get_logger

logger = get_logger(__name__)

#: First restart delay; doubled per quick death up to the cap below.
RESTART_INITIAL_SECONDS = 1.0
#: Backoff ceiling. A run that lasted at least this long resets the backoff.
RESTART_MAX_SECONDS = 60.0


class _WorkerApp(Protocol):
    async def run_worker_async(self, **kwargs) -> None: ...


def next_delay(previous: float, *, ran_for: float, initial: float, maximum: float) -> float:
    """Return the wait before the next restart.

    ``previous`` is the last wait used (``0.0`` before the first restart). A run
    that outlasted ``maximum`` was healthy, so the backoff a boot loop earned
    earlier no longer applies.
    """
    if previous <= 0.0 or ran_for >= maximum:
        return initial
    return min(previous * 2, maximum)


class WorkerSupervisor:
    """Runs ``run_worker_async`` until ``stop`` is set, restarting it with backoff.

    ``alive`` is true while a run is in progress, and is what ``/ready`` reports
    as ``queue``. ``restarts`` counts runs started after the first.
    """

    def __init__(
        self,
        proc_app: _WorkerApp,
        *,
        stop: asyncio.Event,
        initial: float = RESTART_INITIAL_SECONDS,
        maximum: float = RESTART_MAX_SECONDS,
    ) -> None:
        self._app = proc_app
        self._stop = stop
        self._initial = initial
        self._maximum = maximum
        self.alive = False
        self.restarts = 0
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        """Supervise until ``stop`` is set. A cancel propagates, mid-run or mid-wait."""
        loop = asyncio.get_running_loop()
        delay = 0.0
        while True:
            started = loop.time()
            error: Exception | None = None
            self.alive = True
            try:
                await self._app.run_worker_async(install_signal_handlers=False)
            except Exception as exc:  # CancelledError is a BaseException: it propagates
                error = exc
            finally:
                self.alive = False
            if self._stop.is_set():
                return  # orderly shutdown

            delay = next_delay(
                delay, ran_for=loop.time() - started, initial=self._initial, maximum=self._maximum
            )
            logger.error(
                "procrastinate worker %s — no job runs and no periodic task is deferred "
                "until it is back; restarting in %gs",
                "died" if error is not None else "stopped unexpectedly",
                delay,
                exc_info=error,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stop arrived during the backoff
            except TimeoutError:
                pass
            self.restarts += 1


def start_worker(
    proc_app: _WorkerApp,
    *,
    stop: asyncio.Event,
    initial: float = RESTART_INITIAL_SECONDS,
    maximum: float = RESTART_MAX_SECONDS,
) -> WorkerSupervisor:
    """Spawn the supervised worker as a lifespan task (caller owns ``stop``).

    The task is on the returned supervisor's ``task``.
    """
    supervisor = WorkerSupervisor(proc_app, stop=stop, initial=initial, maximum=maximum)
    supervisor.task = asyncio.create_task(supervisor.run(), name="procrastinate-worker")
    return supervisor
