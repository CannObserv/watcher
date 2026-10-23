"""The rehearsal's scratch databases: named, dropped, swept (#383).

The failure this guards was reproduced on demand against PostgreSQL 16.15:
``DROP DATABASE … WITH (FORCE)`` by a non-superuser refuses an autovacuum
worker (its ``PGPROC.roleId`` is ``InvalidOid``, which no role has the
privileges of) with exactly the ``42501`` below, while a plain ``DROP``
SIGTERMs the worker and succeeds. CI's Postgres runs as a superuser, so only
these unit tests reach the fallback there.
"""

import os
import subprocess
import warnings

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL
from tests.ops import scratch_db
from tests.ops.scratch_db import ScratchDatabaseLeaked, with_database

NS = 4026531836
PID = 4242

TERMINATE_REFUSED = (
    "permission denied to terminate process",
    "Only roles with privileges of the role whose process is being terminated or with "
    'privileges of the "pg_signal_backend" role may terminate this process.',
)


class PgError(Exception):
    """What SQLAlchemy's asyncpg adapter puts on ``.orig``: the sqlstate rides along."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def refused(statement: str) -> ProgrammingError:
    """The 16.15 server's answer to FORCE with an autovacuum worker on the database."""
    return ProgrammingError(statement, None, PgError("42501", "\n".join(TERMINATE_REFUSED)))


def in_use(statement: str) -> OperationalError:
    """A plain DROP that waited ~5 s and still found another backend."""
    message = f'database "{statement.split()[4]}" is being accessed by other users'
    return OperationalError(statement, None, PgError("55006", message))


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows

    def scalars(self) -> "Result":
        return Result([row[0] for row in self._rows])


class FakeConn:
    """Answers each statement from a script, in order; records what it was sent.

    A script entry is an exception factory (called with the SQL, then raised),
    a list of rows (returned) or ``None`` (an empty result).
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.sent: list[str] = []

    async def execute(self, statement, params=None) -> Result:
        sql = str(statement)
        self.sent.append(sql)
        answer = self.script.pop(0) if self.script else None
        if callable(answer):
            raise answer(sql)
        return Result(answer or [])


def forced(name: str) -> str:
    return f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"


def plain(name: str) -> str:
    return f"DROP DATABASE IF EXISTS {name}"


NAME = f"watcher_rh{NS}_{PID}_src_test"


class TestNames:
    def test_carry_the_pid_namespace_and_the_pid(self) -> None:
        assert scratch_db.names(pid=PID, ns=NS) == (
            f"watcher_rh{NS}_{PID}_src_test",
            f"watcher_rh{NS}_{PID}_dst_test",
        )

    def test_default_to_this_process(self) -> None:
        assert scratch_db.names() == scratch_db.names(
            pid=os.getpid(), ns=scratch_db.pid_namespace()
        )

    def test_the_namespace_is_this_processs_pid_namespace(self) -> None:
        assert scratch_db.pid_namespace() == os.stat("/proc/self/ns/pid").st_ino


class TestStale:
    def test_is_this_namespaces_dead_pids_and_nothing_else(self) -> None:
        candidates = [
            f"watcher_rh{NS}_111_src_test",  # dead, ours: stale
            f"watcher_rh{NS}_111_dst_test",  # dead, ours: stale
            f"watcher_rh{NS}_222_src_test",  # alive
            f"watcher_rh{NS}_{PID}_src_test",  # this run, whatever alive() says
            "watcher_rh999_111_src_test",  # another pid namespace
            "watcher_rh111_src_test",  # the pre-#383 shape: never swept
            "watcher_test",
            f"watcher_rh{NS}_111_src_test_x",
            f"xwatcher_rh{NS}_111_src_test",
            f"watcher_rh{NS}_111_mid_test",
        ]
        stale = scratch_db.stale(candidates, ns=NS, own_pid=PID, alive=lambda pid: pid == 222)
        assert stale == [f"watcher_rh{NS}_111_src_test", f"watcher_rh{NS}_111_dst_test"]


class TestIsAlive:
    def test_this_process_is(self) -> None:
        assert scratch_db.is_alive(os.getpid())

    def test_a_reaped_child_is_not(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        assert not scratch_db.is_alive(child.pid)

    def test_a_pid_no_process_could_have_is_not(self) -> None:
        """A hand-made name can carry one; setup's sweep must not crash on it."""
        assert not scratch_db.is_alive(10**30)

    def test_a_process_we_may_not_signal_still_is(self, monkeypatch) -> None:
        def kill(pid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(scratch_db.os, "kill", kill)
        assert scratch_db.is_alive(1)


class TestDrop:
    async def test_is_one_forced_drop_when_nothing_resists(self) -> None:
        conn = FakeConn(None)
        assert await scratch_db.drop(conn, NAME) is True
        assert conn.sent == [forced(NAME)]

    async def test_a_backend_the_role_cannot_signal_falls_back_to_a_plain_drop(self) -> None:
        """The #383 failure: FORCE refuses the autovacuum worker, plain DROP
        signals it itself — no warning, no retry."""
        conn = FakeConn(refused, None)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert await scratch_db.drop(conn, NAME) is True
        assert conn.sent == [forced(NAME), plain(NAME)]

    async def test_a_stray_connection_with_the_worker_clears_on_the_next_pass(self) -> None:
        """Plain DROP SIGTERMs the worker but cannot end the role's own stray
        connection (55006); the next FORCE can, once the worker is gone."""
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        conn = FakeConn(refused, in_use, None)
        assert await scratch_db.drop(conn, NAME, sleep=sleep) is True
        assert conn.sent == [forced(NAME), plain(NAME), forced(NAME)]
        assert sleeps == [scratch_db.BACKOFF_SECONDS]

    async def test_a_terminated_backend_slow_to_exit_is_retried_not_raised(self) -> None:
        """FORCE's own 55006: it signalled everyone, but one outlived the ~5 s wait."""
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        conn = FakeConn(in_use, None)
        assert await scratch_db.drop(conn, NAME, sleep=sleep) is True
        assert conn.sent == [forced(NAME), forced(NAME)]
        assert sleeps == [scratch_db.BACKOFF_SECONDS]

    async def test_a_database_that_stays_in_use_warns_naming_it_and_who_holds_it(self) -> None:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # What the test role sees of an autovacuum worker: no usename, no backend_type.
        holder = (598125, None, None, "<insufficient privilege>")
        conn = FakeConn(*[refused, in_use] * scratch_db.ATTEMPTS, [holder])
        with pytest.warns(ScratchDatabaseLeaked, match=NAME) as caught:
            assert await scratch_db.drop(conn, NAME, sleep=sleep) is False

        message = str(caught[0].message)
        assert "598125" in message
        assert "55006" in message
        assert conn.sent[: 2 * scratch_db.ATTEMPTS] == [forced(NAME), plain(NAME)] * (
            scratch_db.ATTEMPTS
        )
        assert "pg_stat_activity" in conn.sent[-1]
        assert len(sleeps) == scratch_db.ATTEMPTS - 1
        assert sleeps == sorted(sleeps) and sleeps[0] == scratch_db.BACKOFF_SECONDS

    async def test_any_other_refusal_of_force_is_raised(self) -> None:
        def syntax(statement: str) -> ProgrammingError:
            return ProgrammingError(statement, None, PgError("42601", "syntax error"))

        conn = FakeConn(syntax)
        with pytest.raises(ProgrammingError):
            await scratch_db.drop(conn, NAME)
        assert conn.sent == [forced(NAME)]

    async def test_a_plain_drop_refused_on_privilege_is_raised_not_retried(self) -> None:
        """Plain DROP never terminates anyone, so its 42501 is "must be owner":
        a wrong role, not a transient — #388's cutover would hit it."""

        def not_owner(statement: str) -> ProgrammingError:
            return ProgrammingError(statement, None, PgError("42501", "must be owner"))

        conn = FakeConn(refused, not_owner)
        with pytest.raises(ProgrammingError):
            await scratch_db.drop(conn, NAME)
        assert conn.sent == [forced(NAME), plain(NAME)]

    @pytest.mark.parametrize(
        "name",
        ["watcher_test", "watcher_rh311328_src_test", f"{NAME}; DROP DATABASE watcher", ""],
    )
    async def test_refuses_any_name_without_the_scratch_shape(self, name: str) -> None:
        conn = FakeConn()
        with pytest.raises(ValueError, match="scratch"):
            await scratch_db.drop(conn, name)
        assert conn.sent == []


class TestDropAll:
    async def test_tries_every_name_then_raises_the_first_error(self) -> None:
        """One broken drop must not leak the other database too."""
        other = f"watcher_rh{NS}_{PID}_dst_test"

        def syntax(statement: str) -> ProgrammingError:
            return ProgrammingError(statement, None, PgError("42601", "syntax error"))

        conn = FakeConn(syntax, None)
        with pytest.raises(ProgrammingError):
            await scratch_db.drop_all(conn, (NAME, other))
        assert conn.sent == [forced(NAME), forced(other)]


class TestSweep:
    async def test_drops_the_stale_and_returns_their_names(self) -> None:
        stale = f"watcher_rh{NS}_111_src_test"
        listed = [(stale,), (f"watcher_rh{NS}_{PID}_src_test",), ("watcher_rh111_dst_test",)]
        conn = FakeConn(listed, None)
        swept = await scratch_db.sweep(conn, ns=NS, own_pid=PID, alive=lambda pid: False)
        assert swept == [stale]
        assert "pg_has_role" in conn.sent[0], "only what this role could drop"
        assert conn.sent[1:] == [forced(stale)]


# --- Against the test server -------------------------------------------------


class Recording:
    """A real connection that also records what it was sent."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.sent: list[str] = []

    async def execute(self, statement, params=None):
        self.sent.append(str(statement))
        return await self._conn.execute(statement, params)


async def _exists(conn, name: str) -> bool:
    found = await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
    return found.scalar() is not None


@pytest.mark.integration
async def test_drop_ends_the_roles_own_stray_connection() -> None:
    """A test that fails before ``dispose()`` leaves this role connected; the
    forced drop is what ends it (a plain one would wait 5 s and fail)."""
    name = scratch_db.names(pid=scratch_db.dead_pid())[0]
    admin = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    stray = create_async_engine(with_database(TEST_DATABASE_URL, name))
    held = None
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE {name}"))
        held = await stray.connect()
        await held.execute(text("SELECT 1"))
        async with admin.connect() as conn:
            recorded = Recording(conn)
            assert await scratch_db.drop(recorded, name) is True
            assert recorded.sent == [forced(name)], "FORCE alone should have ended it"
            assert not await _exists(conn, name)
    finally:
        if held is not None:
            await held.invalidate()  # the drop terminated it
        await stray.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await admin.dispose()


@pytest.mark.integration
async def test_sweep_drops_a_dead_runs_databases_and_spares_the_rest() -> None:
    dead = scratch_db.names(pid=scratch_db.dead_pid())
    live = scratch_db.names(pid=os.getpid())[1:]  # this run's own: spared
    legacy = (f"watcher_rh{scratch_db.dead_pid()}_src_test",)  # the pre-#383 shape: spared
    admin = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            for name in (*dead, *live, *legacy):
                await conn.execute(text(f"CREATE DATABASE {name}"))
            swept = await scratch_db.sweep(conn)
            assert set(dead) <= set(swept)
            assert not set(swept) & {*live, *legacy}
            for name in dead:
                assert not await _exists(conn, name)
            for name in (*live, *legacy):
                assert await _exists(conn, name)
    finally:
        async with admin.connect() as conn:
            for name in (*dead, *live, *legacy):
                await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await admin.dispose()
