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
import pathlib
import subprocess
import sys

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.core import bus, read_windows
from src.core.bus import BUS_ENABLED_ENV, BUS_REDIS_URL_ENV

# The most an unreachable broker may cost before "run the probe detached" in
# src/api/main.py stops being a preference and becomes the only option.
#
# Independent of SOCKET_TIMEOUT_SECONDS, which it happens to equal today. That
# one is derived from the blocking-read window; this one is a judgement about
# how long a lifespan may block. Nothing should move them together.
_INLINE_PROBE_CEILING_SECONDS = 10.0


def _modules_mentioning_block_ms() -> list[str]:
    """Every module under ``src/`` whose source contains ``BLOCK_MS`` at all.

    A **text** scan, deliberately over-matching, used only to decide which
    modules are worth importing. CR round 1, finding 2: the first version walked
    ``pkgutil.iter_modules(src.workers.__path__)``, which is non-recursive *and*
    scoped to one package — so a consumer added under a subpackage, or anywhere
    in ``src/core/`` (five subpackages already), defined a window this never saw.
    A guard whose stated scope is wider than its implemented scope is worse than
    an explicit list, because it reads as complete.

    Matching the bare token rather than an assignment is the other half of that
    fix: ``BLOCK_MS = read_windows.BLOBS_BLOCK_MS`` is an assignment, but
    ``from ... import X as BLOCK_MS`` is not, and a regex precise enough to
    exclude comments would miss the alias. Over-matching costs one import of a
    module that turns out not to define the name (``src/api/main.py`` mentions it
    in a comment); under-matching costs the whole invariant.
    """
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    modules: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if "BLOCK_MS" not in path.read_text(encoding="utf-8"):
            continue
        parts = path.relative_to(src_root.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    return modules


def _blocking_read_windows_ms() -> dict[str, int]:
    """Every ``BLOCK_MS`` actually defined anywhere under ``src/``.

    Discovered rather than listed. The invariant below is only as good as its
    knowledge of the loops, and Watcher's two windows already live in *different
    modules* that can drift apart — a third consumer with a longer window is
    exactly the change that would otherwise slip past, silently, since the
    symptom is a spurious timeout on an idle read rather than anything a reader
    would attribute here.
    """
    found: dict[str, int] = {}
    for name in _modules_mentioning_block_ms():
        window = getattr(importlib.import_module(name), "BLOCK_MS", None)
        if window is not None:
            found[name] = window
    return found


class TestBlockingReadDiscovery:
    def test_discovery_finds_the_known_blocking_loops(self) -> None:
        """Guard the guard: a discovery helper that finds nothing passes vacuously."""
        windows = _blocking_read_windows_ms()
        assert {"src.workers.fetch_facts", "src.workers.registry_reconcile"} <= set(windows)
        assert all(w > 0 for w in windows.values())

    def test_the_scan_reaches_outside_src_workers(self) -> None:
        """The scan's *reach* is the thing under test, not its current yield.

        CR round 1, finding 2: the walk it replaced was non-recursive and
        confined to ``src.workers``, so nothing would have flagged a consumer
        placed one directory deeper. Asserting only on today's two modules would
        pass just as happily under the old walk, so this asserts on the property
        that differs — the scan considers a module under ``src/core/``, which is
        where ``src.core.bus`` itself lives. Depth is proven separately, by
        planting one three levels down and finding it.
        """
        considered = _modules_mentioning_block_ms()
        assert "src.core.read_windows" in considered, "the scan never looked outside src.workers"

    def test_a_window_defined_outside_src_workers_is_discovered(self, tmp_path) -> None:
        """Plant one where the old walk could not see it and prove it is found.

        Written into ``src/core/`` (nested, and not the workers package) and
        removed again, because the guard's value is entirely about the module
        that does not exist yet.
        """
        planted = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src"
            / "core"
            / "notifications"
            / "_cr_probe_block_ms.py"
        )
        planted.write_text("BLOCK_MS = 99_000\n", encoding="utf-8")
        try:
            windows = _blocking_read_windows_ms()
        finally:
            planted.unlink()
        assert windows.get("src.core.notifications._cr_probe_block_ms") == 99_000

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
        # ``cwd`` pinned to the repo root (CR round 1, finding 4): the
        # subprocess inherits pytest's working directory, and ``import src...``
        # resolves only because that happens to be the root. Run from anywhere
        # else, ``check=True`` would raise ``CalledProcessError`` and report an
        # opaque subprocess traceback instead of the layering violation this
        # exists to name — the exact diagnosis the leaf split was meant to spare
        # someone.
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=pathlib.Path(__file__).resolve().parents[2],
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

        The library's default depends on *which constructor* is used, which is
        the trap (CR round 1, finding 5). On the ``from_url`` path this code
        takes, the connection-level default really is zero with ``NoBackoff`` —
        an object that reads as a policy while behaving as none. But
        ``Redis(host=...)`` defaults to ``Retry(ExponentialWithJitterBackoff(
        base=1, cap=10), retries=3)``, so the same "harmless default" reasoning
        would be wrong there. Passing the zero explicitly makes it a decision a
        future change has to argue with on either path.
        """
        client = bus.bus_client_from_env()
        assert bus.BUS_RETRIES == 0
        # Private attribute: redis-py publishes no accessor for the retry count.
        assert client.connection_pool.make_connection().retry._retries == 0
        await client.aclose()

    async def test_the_constructor_default_is_not_zero_so_the_explicit_one_matters(self) -> None:
        """Pin the asymmetry the comment above rests on (CR round 1, finding 5).

        If redis-py ever makes the two constructors agree, the explicit zero
        stops being load-bearing and this says so instead of leaving a warning
        that has quietly become false.
        """
        assert Redis().get_connection_kwargs()["retry"]._retries == 3

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
            # CR round 1, finding 1: redis-py's ``from_url`` also accepts the
            # credential as a *query argument*, and both forms parse into real
            # ``connection_kwargs``. Redacting only userinfo let these through
            # verbatim — an open failure in the one control whose whole job is to
            # fail closed.
            ("redis://broker:6379/0?password=hunter2", "redis://broker:6379/0?password=***"),
            (
                "rediss://broker:6380/0?ssl_password=sslsecret",
                "rediss://broker:6380/0?ssl_password=***",
            ),
            # Both halves of a URL that carries the credential twice.
            (
                "redis://watcher:hunter2@broker:6379/0?password=hunter2",
                "redis://watcher:***@broker:6379/0?password=***",
            ),
            # Non-secret query arguments survive, and so does their order.
            (
                "redis://broker:6379/0?password=hunter2&client_name=watcher",
                "redis://broker:6379/0?password=***&client_name=watcher",
            ),
            (
                "redis://broker:6379/0?client_name=watcher",
                "redis://broker:6379/0?client_name=watcher",
            ),
        ],
    )
    def test_redact_url_removes_the_password(self, url: str, expected: str) -> None:
        assert bus.redact_url(url) == expected

    def test_every_secret_bearing_query_argument_redis_py_accepts_is_covered(self) -> None:
        """Enumerate the leak surface from the library, not from memory.

        ``password`` and ``ssl_password`` are the two connection kwargs carrying
        a secret that ``from_url`` will take off a query string (``ssl_keyfile``
        is a path, and ``credential_provider`` is an object no URL can express).
        Asserting against the *matcher* rather than a literal pair means a future
        redis-py that adds another ``*_password`` is covered on arrival.
        """
        for name in ("password", "ssl_password"):
            assert bus._is_secret_query_key(name)
        assert not bus._is_secret_query_key("client_name")
        assert not bus._is_secret_query_key("ssl_keyfile")

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
