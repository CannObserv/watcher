"""Tests for the FastAPI lifespan.

Nothing to pre-warm since #254: the Archiver SDK went with Watcher's last
outbound call to Archiver, so what remains to assert is the consumers — which
start, and that they stop.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.core.bus import BUS_ENABLED_ENV, BUS_REDIS_URL_ENV, BusNotEnabled

# Named here, not imported: the module that read it is gone (#376 part 2).
_NOTIFY_CUTOVER_AT_ENV = "WATCHER_NOTIFY_CUTOVER_AT"


@pytest.mark.asyncio
async def test_lifespan_does_not_start_changes_drain(monkeypatch):
    """Lifespan should not start a changes-drain loop after Phase 5 cutover."""
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import app

        async with app.router.lifespan_context(app):
            for attr in dir(app.state):
                assert "changes_drain" not in attr.lower()
                assert "drain_changes" not in attr.lower()


@pytest.mark.asyncio
async def test_registry_consumer_is_dormant_without_a_bus_url(monkeypatch):
    """Done-when #1 (#254): no ``WATCHER_BUS_REDIS_URL`` → no consumer at all.

    Dormant, not degraded-and-noisy: the process still serves, and the error log
    beside it says why the registry cannot reconcile.
    """
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run
    start_registry = MagicMock()

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
        patch("src.api.main.start_registry_consumer", start_registry),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    start_registry.assert_not_called()


@pytest.mark.asyncio
async def test_registry_consumer_starts_and_stops_with_a_bus_url(monkeypatch):
    """With a bus, the registry inbox starts beside the fact inbox and is
    cancelled on shutdown — an un-cancelled task would hold the process open for
    a full read block."""
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    async def _forever() -> None:
        await asyncio.Event().wait()

    registry_task = asyncio.create_task(_forever())
    blobs_task = asyncio.create_task(_forever())
    derived_task = asyncio.create_task(_forever())
    watch_check_task = asyncio.create_task(_forever())
    start_watch_check = MagicMock(return_value=watch_check_task)

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=MagicMock()),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
        patch("src.api.main.start_blobs_consumer", MagicMock(return_value=blobs_task)),
        patch("src.api.main.start_registry_consumer", MagicMock(return_value=registry_task)),
        patch("src.api.main.start_derived_consumer", MagicMock(return_value=derived_task)),
        patch("src.api.main.start_watch_check_consumer", start_watch_check),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass

    assert registry_task.cancelled()
    assert blobs_task.cancelled()
    # #325: the content.derived inbox runs beside content.blobs, same lifecycle.
    assert derived_task.cancelled()
    # #371: the info.watch-check command inbox, same lifecycle.
    start_watch_check.assert_called_once()
    assert watch_check_task.cancelled()


@pytest.mark.asyncio
async def test_lifespan_refuses_a_bus_url_without_the_opt_in(monkeypatch, caplog):
    """#262: ``WATCHER_BUS_REDIS_URL`` set and ``WATCHER_BUS_ENABLED`` absent aborts.

    Deliberately sets the variable that ``tests/conftest.py`` clears at import.
    Without that, nothing in the suite reaches this branch — the flag is inert
    under pytest, so its first exercise would otherwise be a production restart.

    The refusal is logged CRITICAL before it propagates: under systemd a bare
    lifespan traceback buries the actionable line in journalctl, exactly as the
    production-database guard beside it found (#233).
    """
    monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
    monkeypatch.delenv(BUS_ENABLED_ENV, raising=False)

    with (
        patch("src.api.main.get_app") as get_app,
        patch("src.api.main.get_shared_bus_client") as get_client,
    ):
        from src.api.main import lifespan

        with caplog.at_level("CRITICAL", logger="src.api.main"):
            with pytest.raises(BusNotEnabled):
                async with lifespan(MagicMock()):
                    pass

    # Nothing was built: the refusal precedes every resource, so a refused
    # process never joins the consumer group or opens the worker.
    get_client.assert_not_called()
    get_app.assert_not_called()
    assert any(BUS_ENABLED_ENV in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_lifespan_accepts_a_bus_url_with_the_opt_in(monkeypatch):
    """The sanctioned production shape — the unit sets both — still starts."""
    monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/0")
    monkeypatch.setenv(BUS_ENABLED_ENV, "1")

    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import lifespan

        async with lifespan(MagicMock()):
            pass


def _idle_proc_app():
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fake_proc_app.run_worker_async = _worker_run
    return fake_proc_app


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["2026-11-06T21:00:00Z", "2026-11-06", "not-an-instant"])
async def test_a_leftover_notify_cutover_line_is_ignored(monkeypatch, caplog, value):
    """#376 part 2: the T gate is gone with Watcher's dispatch. The line stays in
    ``/etc/watcher/.env`` until the operator removes it after this deploy, so
    whatever it holds starts the process and says nothing."""
    monkeypatch.setenv(_NOTIFY_CUTOVER_AT_ENV, value)

    with (
        patch("src.api.main.get_app", return_value=_idle_proc_app()),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import app

        with caplog.at_level("INFO"):
            async with app.router.lifespan_context(app):
                pass

    assert not [r for r in caplog.records if "notify cutover" in r.getMessage()]


# Named here, not imported: #379 deleted every module that read them. The
# values are fixtures, never a real host or key.
_LEFTOVER_NOTIFICATION_SETTINGS = (
    {"WATCHER_NOTIFIER_BASE_URL": "http://notifier.invalid:9000"},
    {"WATCHER_NOTIFIER_ENABLED": "1"},
    {"WATCHER_NOTIFIER_BASE_URL": "http://notifier.invalid:9000", "WATCHER_NOTIFIER_API_KEY": "k"},
    {"WATCHER_PUBLIC_BASE_URL": "not a url"},
    {"WATCHER_DIFF_MAX_INPUT_BYTES": "-1"},
    {"GCS_BLOB_CREDENTIALS": "/nonexistent/key.json"},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    _LEFTOVER_NOTIFICATION_SETTINGS,
    ids=["url-without-flag", "flag-without-url", "url-key-no-flag", "bad-base", "diff", "gcs"],
)
async def test_leftover_notification_settings_are_ignored(monkeypatch, caplog, settings):
    """#379: the notifier gates (#277, #278), the public-base check (#296 D6) and
    the diff's settings went with the subsystem. Until the operator installs the
    new unit and clears ``/etc/watcher/.env``, the service may still be handed
    any of them, in combinations the old lifespan refused; each now starts the
    process, and nothing names them."""
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    with (
        patch("src.api.main.get_app", return_value=_idle_proc_app()),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import app

        with caplog.at_level("INFO"):
            async with app.router.lifespan_context(app):
                pass

    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert not [r for r in caplog.records if any(name in r.getMessage() for name in settings)]


class TestBusReachabilityProbe:
    """#287: ``from_url`` is lazy, so startup cannot otherwise tell a reachable
    broker from a dead one. The consumers start either way and a partition then
    presents exactly as an idle cluster — which is also what a healthy Watcher
    with nothing to do looks like. One PING at ERROR closes that gap."""

    @staticmethod
    def _proc_app():
        fake = MagicMock()
        fake.open_async = AsyncMock()
        fake.close_async = AsyncMock()

        async def _worker_run(
            install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
        ) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

        fake.run_worker_async = _worker_run
        return fake

    @pytest.mark.asyncio
    async def test_the_probe_does_not_block_the_lifespan(self, monkeypatch):
        """Detached, not awaited. The worst case is the cold-boot window
        (``BOOT_REACHABILITY_WINDOW_SECONDS``, #296) plus one connect, and the
        dashboard has no business being unavailable because the bus is — so a
        broker that never answers must not hold the HTTP surface closed.
        """
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://:hunter2@broker:6379/0")
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")

        pinged = asyncio.Event()

        class _Hangs:
            async def ping(self):
                pinged.set()
                await asyncio.Event().wait()  # never answers

        forever = asyncio.create_task(asyncio.Event().wait())

        with (
            patch("src.api.main.get_app", return_value=self._proc_app()),
            patch("src.api.main.get_shared_bus_client", return_value=_Hangs()),
            patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
            patch("src.api.main.start_blobs_consumer", MagicMock(return_value=forever)),
            patch("src.api.main.start_registry_consumer", MagicMock(return_value=forever)),
        ):
            from src.api.main import lifespan

            application = MagicMock()
            async with lifespan(application):
                # Entered while the PING is still outstanding: that is the whole
                # claim. Awaiting the event would pass even if it were inline.
                await asyncio.wait_for(pinged.wait(), timeout=5)
                task = application.state.bus_reachability_task
                assert not task.done()

        # Shutdown joins it rather than leaving a "Task was destroyed but it is
        # pending" behind — the one case a straggling PING actually matters.
        assert task.done()

    @pytest.mark.asyncio
    async def test_an_unreachable_broker_is_logged_with_the_password_redacted(
        self, monkeypatch, caplog
    ):
        """The URL is the one field identifying *which* broker is down, and the
        broker gains a ``requirepass`` at CannObserv/broker#1 D3. This line goes
        to journald, which is the one place an operator is guaranteed to look."""
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://watcher:hunter2@broker:6379/0")
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")
        # No cold-boot grace (#296): this asserts what the ERROR line carries,
        # not when it arrives, and the real window would make the test wait it
        # out. The retry itself is tested in tests/core/test_bus_client_policy.py.
        monkeypatch.setattr("src.core.bus.BOOT_REACHABILITY_WINDOW_SECONDS", 0.0)

        class _Dead:
            async def ping(self):
                # The redis exception, not the builtin of the same name (CR
                # round 1, finding 7): with the builtin this would keep passing
                # if the probe ever narrowed its ``except`` to redis errors,
                # which is the change most likely to break it.
                raise RedisConnectionError("Error 111 connecting to broker:6379.")

        forever = asyncio.create_task(asyncio.Event().wait())

        with (
            patch("src.api.main.get_app", return_value=self._proc_app()),
            patch("src.api.main.get_shared_bus_client", return_value=_Dead()),
            patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
            patch("src.api.main.start_blobs_consumer", MagicMock(return_value=forever)),
            patch("src.api.main.start_registry_consumer", MagicMock(return_value=forever)),
            caplog.at_level("ERROR", logger="src.core.bus"),
        ):
            from src.api.main import lifespan

            application = MagicMock()
            async with lifespan(application):
                assert await application.state.bus_reachability_task is False

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "unreachable" in messages
        assert "hunter2" not in repr(caplog.records)

    @pytest.mark.asyncio
    async def test_no_bus_client_means_no_probe(self, monkeypatch):
        """Dormant is not down. With no client there is no broker to be
        unreachable, and the existing "consumers NOT started" ERROR already says
        why nothing will arrive."""
        with (
            patch("src.api.main.get_app", return_value=self._proc_app()),
            patch("src.api.main.get_shared_bus_client", return_value=None),
            patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
            patch("src.api.main.probe_bus_reachable") as probe,
        ):
            from src.api.main import lifespan

            async with lifespan(MagicMock()):
                pass

        probe.assert_not_called()


@pytest.mark.asyncio
async def test_the_worker_runs_supervised_and_stops_with_the_lifespan():
    """#340: the worker runs under the supervisor on ``app.state`` — which
    ``/ready`` reads — and shutdown ends it rather than restarting it."""
    started = asyncio.Event()
    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock()
    fake_proc_app.close_async = AsyncMock()

    async def _worker_run(
        install_signal_handlers: bool = True, shutdown_graceful_timeout: float | None = None
    ) -> None:
        started.set()
        await asyncio.Event().wait()

    fake_proc_app.run_worker_async = _worker_run

    with (
        patch("src.api.main.get_app", return_value=fake_proc_app),
        patch("src.api.main.get_shared_bus_client", return_value=None),
        patch("src.api.main.aclose_shared_bus_client", AsyncMock()),
    ):
        from src.api.main import lifespan

        application = MagicMock()
        async with lifespan(application):
            await asyncio.wait_for(started.wait(), timeout=5)
            supervisor = application.state.worker_supervisor
            assert supervisor.alive

    assert supervisor.task.done()
    assert not supervisor.alive
    assert supervisor.restarts == 0
