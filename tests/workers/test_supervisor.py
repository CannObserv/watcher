"""Tests for the embedded worker's supervisor (#340).

A Postgres cluster restart kills procrastinate's LISTEN connection, and its
reconnect has no retry (``PsycopgConnector.listen_notify``, unchanged through
3.10.0): the ``listener`` side task fails, ``Worker._monitor_side_tasks`` calls
``stop()``, and ``run_worker_async`` *returns normally*. Nothing awaited the
task, so every periodic task stopped with the process still serving.

The chain is driven here through a real ``procrastinate.App`` on its own
``InMemoryConnector``, failing exactly where the reproduction on #338's scratch
cluster failed — so a procrastinate upgrade that changes the shape of that
failure changes these tests' outcome too.
"""

import asyncio
import logging

import procrastinate
from procrastinate.exceptions import ConnectorException
from procrastinate.testing import InMemoryConnector

from src.workers.supervisor import WorkerSupervisor, next_delay, start_worker

FAST = {"initial": 0.01, "maximum": 0.05}


class _ListenerDiesOnce(InMemoryConnector):
    """The reproduction: the first LISTEN connection's reconnect raises."""

    def __init__(self) -> None:
        super().__init__()
        self.listens = 0

    async def listen_notify(self, on_notification, channels) -> None:
        self.listens += 1
        if self.listens == 1:
            raise ConnectorException("Database error.")
        await super().listen_notify(on_notification, channels)


class _RegisterFailsOnce(InMemoryConnector):
    """#340's second path: ``register_worker`` raises at boot (no schema, #341)."""

    def __init__(self) -> None:
        super().__init__()
        self.registrations = 0

    async def register_worker_one(self):
        self.registrations += 1
        if self.registrations == 1:
            raise ConnectorException('relation "procrastinate_workers" does not exist')
        return await super().register_worker_one()


async def _until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def _supervisor_errors(caplog) -> list[logging.LogRecord]:
    """Watcher's own lines; procrastinate logs ``side_task_failed`` itself, and
    that line alone is what the reproduction saw before the worker went quiet."""
    return [
        r
        for r in caplog.records
        if r.name == "src.workers.supervisor" and r.levelno == logging.ERROR
    ]


async def _shutdown(stop: asyncio.Event, task: asyncio.Task) -> None:
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_listener_death_is_logged_and_the_worker_restarts(caplog):
    """The red test #340 asked for: the worker ends, and watcher notices."""
    connector = _ListenerDiesOnce()
    app = procrastinate.App(connector=connector)
    stop = asyncio.Event()

    async with app.open_async():
        with caplog.at_level(logging.ERROR, logger="src.workers.supervisor"):
            supervisor = start_worker(app, stop=stop, **FAST)
            try:
                await _until(lambda: supervisor.restarts >= 1 and supervisor.alive)
                # The second run registered a live worker: periodic deferral and
                # job fetching are back without a process restart.
                await _until(lambda: len(connector.workers) == 1)
            finally:
                await _shutdown(stop, supervisor.task)

    errors = _supervisor_errors(caplog)
    assert errors, "the worker ended and watcher logged nothing at ERROR"
    assert "restarting" in errors[0].getMessage()


async def test_a_boot_time_failure_is_logged_at_once_not_at_shutdown(caplog):
    """Before #340 the exception sat in the task until the shutdown ``gather``
    swallowed it with ``return_exceptions=True`` — no line at all."""
    connector = _RegisterFailsOnce()
    app = procrastinate.App(connector=connector)
    stop = asyncio.Event()

    async with app.open_async():
        with caplog.at_level(logging.ERROR, logger="src.workers.supervisor"):
            supervisor = start_worker(app, stop=stop, **FAST)
            try:
                await _until(lambda: connector.registrations >= 2 and len(connector.workers) == 1)
                errors = _supervisor_errors(caplog)
                assert errors, "logged only at shutdown, if at all"
                assert errors[0].exc_info is not None
                assert "procrastinate_workers" in str(errors[0].exc_info[1])
            finally:
                await _shutdown(stop, supervisor.task)


class _FakeApp:
    """Stands in for ``procrastinate.App``; each run ends as scripted."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.runs = 0

    async def run_worker_async(self, **kwargs) -> None:
        assert kwargs == {"install_signal_handlers": False}
        self.runs += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "forever"
        if outcome == "forever":
            await asyncio.Event().wait()
        elif isinstance(outcome, BaseException):
            raise outcome


async def test_an_asked_for_stop_is_not_restarted(caplog):
    """The lifespan sets ``stop`` before it cancels; a worker that returns after
    that is shutting down, not dying."""
    stop = asyncio.Event()
    app = _FakeApp("return")
    stop.set()
    with caplog.at_level(logging.ERROR, logger="src.workers.supervisor"):
        await WorkerSupervisor(app, stop=stop, **FAST).run()
    assert app.runs == 1
    assert not _supervisor_errors(caplog)


async def test_a_cancel_during_the_backoff_propagates():
    """Shutdown mid-backoff must end the task, not start another worker."""
    stop = asyncio.Event()
    app = _FakeApp(RuntimeError("down"))
    supervisor = start_worker(app, stop=stop, initial=60.0, maximum=60.0)
    await _until(lambda: app.runs == 1 and not supervisor.alive)
    supervisor.task.cancel()
    await asyncio.gather(supervisor.task, return_exceptions=True)
    assert supervisor.task.cancelled()
    assert app.runs == 1


async def test_a_cancel_mid_run_propagates():
    stop = asyncio.Event()
    app = _FakeApp()
    supervisor = start_worker(app, stop=stop, **FAST)
    await _until(lambda: supervisor.alive)
    supervisor.task.cancel()
    await asyncio.gather(supervisor.task, return_exceptions=True)
    assert supervisor.task.cancelled()
    assert not supervisor.alive


async def test_alive_tracks_the_run():
    """``/ready`` reads this: false from the moment a run ends until the next
    one starts."""
    stop = asyncio.Event()
    app = _FakeApp(RuntimeError("down"))
    supervisor = start_worker(app, stop=stop, initial=60.0, maximum=60.0)
    try:
        await _until(lambda: app.runs == 1 and not supervisor.alive)
        assert supervisor.restarts == 0
    finally:
        await _shutdown(stop, supervisor.task)


class TestNextDelay:
    def test_the_first_restart_waits_the_initial_delay(self):
        assert next_delay(0.0, ran_for=0.1, initial=1.0, maximum=60.0) == 1.0

    def test_doubles_after_a_quick_death(self):
        assert next_delay(2.0, ran_for=0.1, initial=1.0, maximum=60.0) == 4.0

    def test_is_capped(self):
        assert next_delay(40.0, ran_for=0.1, initial=1.0, maximum=60.0) == 60.0

    def test_resets_after_a_run_that_outlasted_the_cap(self):
        """A worker that ran for hours before a cluster restart gets the quick
        first retry, not the backoff a boot loop earned long ago."""
        assert next_delay(60.0, ran_for=3600.0, initial=1.0, maximum=60.0) == 1.0
