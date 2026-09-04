"""The shared bus client's connection policy (#287, CannObserv/broker#1 R7).

``bus_client_from_env`` was a bare ``Redis.from_url`` with library defaults.
Loopback made that harmless — a local broker either answers in microseconds or
refuses immediately. Neither holds across the tailnet hop to the relocated
broker, where the measured path is a ~40 ms DERP relay, so a *stalled* broker
(as distinct from a refusing one) would hold a consumer read open with no bound.
``get_shared_bus_client`` pins one client for the whole process, and ``/health``
knows nothing about the bus, so the wedge is invisible from outside.

Three measurements from Archiver's half (CannObserv/archiver#193) drive the
shape below, and each contradicted the intuition going in. They are quoted in
the individual tests rather than here, so a reader who changes one constant
lands on the reason for it.
"""

import ast
import asyncio
import importlib
import pkgutil
import subprocess
import sys

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

import src.workers as workers_pkg
from src.core import bus, read_windows
from src.core.bus import BUS_ENABLED_ENV, BUS_REDIS_URL_ENV

# The most an unreachable broker may cost before "run the probe detached" in
# src/api/main.py stops being a preference and becomes the only option.
#
# Independent of SOCKET_TIMEOUT_SECONDS, which it happens to equal today. That
# one is derived from the blocking-read window; this one is a judgement about
# how long a lifespan may block. Nothing should move them together.
_INLINE_PROBE_CEILING_SECONDS = 10.0


def _blocking_read_windows_ms() -> dict[str, int]:
    """Every ``BLOCK_MS`` defined anywhere under ``src.workers``.

    Discovered rather than listed. The invariant below is only as good as its
    knowledge of the loops, and Watcher's two windows already live in *different
    modules* that can drift apart — a third consumer with a longer window is
    exactly the change that would otherwise slip past, silently, since the
    symptom is a spurious timeout on an idle read rather than anything a reader
    would attribute here.
    """
    found: dict[str, int] = {}
    for info in pkgutil.iter_modules(workers_pkg.__path__):
        module = importlib.import_module(f"{workers_pkg.__name__}.{info.name}")
        window = getattr(module, "BLOCK_MS", None)
        if window is not None:
            found[info.name] = window
    return found


class TestBlockingReadDiscovery:
    def test_discovery_finds_the_known_blocking_loops(self) -> None:
        """Guard the guard: a discovery helper that finds nothing passes vacuously."""
        windows = _blocking_read_windows_ms()
        assert {"fetch_facts", "registry_reconcile"} <= set(windows)
        assert all(w > 0 for w in windows.values())

    def test_the_leaf_actually_holds_the_longest_window(self) -> None:
        """``read_windows`` claims to know the longest window; audit the claim.

        Its name is the load-bearing part — ``bus`` trusts it instead of
        surveying the loops itself. A leaf that is *wrong* about being the
        longest is worse than the import it replaced, because the error is
        silent and reads as authoritative.
        """
        assert read_windows.LONGEST_BLOCK_MS == max(_blocking_read_windows_ms().values())

    def test_read_windows_is_actually_a_leaf(self) -> None:
        """The leaf must import nothing else from ``src``.

        That property is the entire reason it exists: ``src.core.bus`` derives
        its socket timeout from it *instead of* importing ``src.workers``, which
        would put client construction downstream of the loops that consume its
        client — and ``src.workers.registry_reconcile`` already imports
        ``src.workers.watch_status``, which imports ``src.core.bus``. That is a
        cycle, not a style preference, and an ``ImportError`` at startup is a
        poor way to discover one.

        Imported in a clean interpreter so the assertion is about what
        ``read_windows`` pulls in, not what an earlier test already loaded.
        """
        code = (
            "import sys\n"
            "from src.core import read_windows\n"
            "print(repr(sorted(m for m in sys.modules if m.startswith('src.'))))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        pulled = ast.literal_eval(result.stdout.strip())
        assert pulled == ["src.core", "src.core.read_windows"], (
            f"read_windows is no longer a leaf — it pulled in {pulled}"
        )


class TestSocketTimeout:
    def test_socket_timeout_exceeds_every_blocking_read_window(self) -> None:
        """``socket_timeout`` has a floor, not a ceiling.

        Measured, not assumed: redis-py does **not** extend ``socket_timeout``
        for a blocking command. On redis-py 7.4.1, a client with
        ``socket_timeout=1`` issuing ``XREAD ... BLOCK 3000`` raised
        ``TimeoutError`` after 1.01 s; the same call at ``socket_timeout=10``
        returned normally after 3.08 s.

        Both Watcher loops block for 5 s, so any ``socket_timeout`` at or below
        that does not bound a stall — it manufactures one on every idle read
        against a perfectly healthy broker.
        """
        longest_s = max(_blocking_read_windows_ms().values()) / 1000
        assert bus.SOCKET_TIMEOUT_SECONDS > longest_s, (
            f"socket_timeout {bus.SOCKET_TIMEOUT_SECONDS}s does not clear the longest "
            f"blocking read window ({longest_s}s); idle reads would time out"
        )
        assert bus.SOCKET_TIMEOUT_SECONDS >= longest_s + bus.BLOCKING_READ_MARGIN_SECONDS

    def test_socket_timeout_is_derived_not_transcribed(self) -> None:
        """Raising a loop's ``BLOCK_MS`` must move the timeout with it.

        A coupling that lives only in a comment beside a literal is one someone
        edits half of — and here the two halves are in three different files.
        """
        assert bus.SOCKET_TIMEOUT_SECONDS == (
            read_windows.LONGEST_BLOCK_MS / 1000 + bus.BLOCKING_READ_MARGIN_SECONDS
        )

    def test_connect_timeout_is_bounded_and_shorter_than_the_read_timeout(self) -> None:
        """A refusing or black-holed broker must fail fast; a *slow* one must not.

        Connecting carries no ``BLOCK``, so it needs no headroom — and a ~40 ms
        relayed path clears a multi-second budget by two orders of magnitude.
        """
        assert 0 < bus.SOCKET_CONNECT_TIMEOUT_SECONDS < bus.SOCKET_TIMEOUT_SECONDS

    def test_worst_case_connect_is_retries_times_the_connect_timeout(self) -> None:
        """``socket_connect_timeout`` bounds one *attempt*, not the call.

        Measured against a black-holed address on redis-py 7.4.1:
        ``connect=5/retries=0`` raised at 5.01 s, ``connect=5/retries=1`` at
        10.03 s, ``connect=2/retries=1`` at 4.02 s. So the budget an operator
        actually waits is ``(retries + 1) x socket_connect_timeout``, and
        reading the connect timeout alone understates it by the retry factor.

        Asserted as a *bound*, not as the product that defines the constant: an
        arithmetic identity could only fail if someone edited one half of one
        expression. What matters is that the budget stays small enough that
        running the probe inline in the lifespan would still be a choice rather
        than a hang.
        """
        assert bus.WORST_CASE_CONNECT_SECONDS <= _INLINE_PROBE_CEILING_SECONDS


class TestPolicyIsApplied:
    """The gate is unchanged; what it hands back now carries a policy."""

    @pytest.fixture
    def allowed_env(self, monkeypatch):
        monkeypatch.setenv(BUS_REDIS_URL_ENV, "redis://localhost:6379/14")
        monkeypatch.setenv(BUS_ENABLED_ENV, "1")

    async def test_env_client_carries_the_whole_policy(self, allowed_env) -> None:
        client = bus.bus_client_from_env()
        assert client is not None
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == bus.SOCKET_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == bus.SOCKET_CONNECT_TIMEOUT_SECONDS
        assert kwargs["health_check_interval"] == bus.HEALTH_CHECK_INTERVAL_SECONDS
        await client.aclose()

    async def test_the_shared_client_carries_it_too(self, allowed_env) -> None:
        """``get_shared_bus_client`` is what the consumers and every producer
        actually hold, so the funnel has to be the thing that applies it."""
        try:
            client = bus.get_shared_bus_client()
            assert client is not None
            assert (
                client.connection_pool.connection_kwargs["socket_timeout"]
                == bus.SOCKET_TIMEOUT_SECONDS
            )
        finally:
            await bus.aclose_shared_bus_client()

    async def test_client_takes_no_retry_because_a_retry_re_sends_the_command(
        self, allowed_env
    ) -> None:
        """Zero retries, and the zero is the load-bearing part.

        A redis-py retry **re-sends the command**; it does not resume a
        response. ``Redis.execute_command`` wraps
        ``_send_command_parse_response`` in ``Retry.call_with_retry``, so a
        ``TimeoutError`` raised *after* the broker already applied an ``XADD``
        publishes the entry a second time.

        Watcher's exposure is on the producer side. Duplicates on the two
        last-write-wins config streams (``content.fetch-policy``,
        ``info.watch-status``) are absorbed by construction, but
        ``content.fetch`` is a command stream with a consumer group, and a
        duplicated command is a duplicated fetch — a second real origin request
        against a government portal under Watcher's pinned User-Agent.

        redis-py's own default ``Retry`` is also zero, which is the trap: the
        object reads as a policy while behaving as none, and ``retry_on_timeout=
        True`` raises it to one. Passing the zero explicitly makes it a decision
        a future change has to argue with.
        """
        client = bus.bus_client_from_env()
        assert bus.BUS_RETRIES == 0
        # Private attribute: redis-py publishes no accessor for the retry count.
        assert client.connection_pool.make_connection().retry._retries == 0
        await client.aclose()

    async def test_the_retryable_set_is_left_at_the_library_default(self, allowed_env) -> None:
        """``retry_on_error`` is deliberately not passed.

        redis-py's ``Retry`` already carries exactly ``(ConnectionError,
        TimeoutError)``, so passing the same pair would imply this widens
        something it does not. Asserted rather than merely omitted, so that if
        the library ever narrows its default the omission stops being safe and
        this says so.
        """
        client = bus.bus_client_from_env()
        supported = set(client.connection_pool.make_connection().retry._supported_errors)
        assert RedisConnectionError in supported
        assert RedisTimeoutError in supported
        await client.aclose()


class TestRedactUrl:
    """The broker gains a ``requirepass`` at CannObserv/broker#1 D3, and every
    line naming the URL goes to journald. A probe that reports "unreachable" by
    printing the URL would publish the password to the one place an operator is
    guaranteed to look."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("redis://localhost:6379/0", "redis://localhost:6379/0"),
            ("redis://:hunter2@broker:6379/0", "redis://:***@broker:6379/0"),
            ("redis://watcher:hunter2@broker:6379/0", "redis://watcher:***@broker:6379/0"),
            ("rediss://watcher:hunter2@broker:6380/0", "rediss://watcher:***@broker:6380/0"),
            ("redis://watcher@broker:6379/0", "redis://watcher@broker:6379/0"),
            # IPv6: urlsplit().hostname strips the brackets, so a naive rebuild
            # emits something that is no longer a URL.
            ("redis://:hunter2@[::1]:6379/0", "redis://:***@[::1]:6379/0"),
            ("redis://u:hunter2@[2001:db8::1]:6379/0", "redis://u:***@[2001:db8::1]:6379/0"),
            # No password: returned verbatim, so the host keeps its original case.
            ("redis://BROKER.Example:6379/0", "redis://BROKER.Example:6379/0"),
        ],
    )
    def test_redact_url_removes_the_password(self, url: str, expected: str) -> None:
        assert bus.redact_url(url) == expected

    def test_redact_url_never_leaks_on_a_url_it_cannot_parse(self) -> None:
        """Fail closed. A redactor that re-raises or passes the input through on
        a malformed URL leaks exactly when something is already wrong."""
        assert "hunter2" not in bus.redact_url("redis://[not-a-valid-url:hunter2@@@")


class TestProbeBusReachable:
    """``from_url`` is **lazy**: it returns against a broker with nothing
    listening and raises nothing. So the lifespan's wiring never sees an
    unreachable broker — the process starts, the loops are scheduled, and the
    first failure lands inside a loop that correctly treats it as transient and
    backs off quietly. Correct for a partition; indistinguishable from an idle
    cluster for a misconfiguration."""

    async def test_probe_logs_error_when_the_broker_is_unreachable(self, monkeypatch) -> None:
        records: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            bus.logger, "error", lambda msg, *a, **k: records.append((msg, k.get("extra", {})))
        )

        class _Dead:
            async def ping(self):
                raise RedisConnectionError("Error 111 connecting to broker:6379.")

        ok = await bus.probe_bus_reachable(_Dead(), "redis://:hunter2@broker:6379/0")

        assert ok is False
        assert len(records) == 1
        assert records[0][1]["redis_url"] == "redis://:***@broker:6379/0"
        assert "hunter2" not in repr(records)

    async def test_probe_logs_info_with_latency_when_reachable(self, monkeypatch) -> None:
        """The success line carries the round trip: the relocated broker is
        across a ~40 ms relay, so "reachable" alone stops being the whole story."""
        records: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            bus.logger, "info", lambda msg, *a, **k: records.append((msg, k.get("extra", {})))
        )

        class _Live:
            async def ping(self):
                return True

        assert await bus.probe_bus_reachable(_Live(), "redis://broker:6379/0") is True
        assert len(records) == 1
        assert records[0][1]["rtt_ms"] >= 0

    async def test_probe_never_raises(self, monkeypatch) -> None:
        """It runs detached, so an exception here would surface as a bare "Task
        exception was never retrieved" and set the diagnosis back."""
        monkeypatch.setattr(bus.logger, "error", lambda *a, **k: None)

        class _Weird:
            async def ping(self):
                raise RuntimeError("something no one anticipated")

        assert await bus.probe_bus_reachable(_Weird(), "redis://broker:6379/0") is False

    async def test_probe_lets_cancellation_through(self, monkeypatch) -> None:
        """``BaseException`` is deliberately not caught: a ``CancelledError`` at
        shutdown must propagate, or the lifespan's gather would never join it."""
        monkeypatch.setattr(bus.logger, "error", lambda *a, **k: None)

        class _Hangs:
            async def ping(self):
                await asyncio.Event().wait()

        task = asyncio.create_task(bus.probe_bus_reachable(_Hangs(), "redis://broker:6379/0"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
