"""Scratch databases for the backup/restore rehearsal: named, dropped, swept (#383).

**Why a plain ``DROP`` backs up ``WITH (FORCE)``.** The autovacuum launcher
visits a freshly seeded database within a minute, and on PostgreSQL 16 a
non-superuser's ``DROP DATABASE … WITH (FORCE)`` refuses an autovacuum worker:
``TerminateOtherDBBackends`` checks ``has_privs_of_role(caller, roleId)``, and
a worker's ``roleId`` is ``InvalidOid``, which no role has the privileges of —
``42501 permission denied to terminate process``, despite that function's own
comment saying FORCE accepts workers. A plain ``DROP`` SIGTERMs the worker
itself and waits up to ~5 s. FORCE still goes first because it is what ends
this role's own stray connections, which a plain ``DROP`` cannot.

**Why the name carries the pid namespace.** ``os.kill(pid, 0)`` answers only
for this namespace, so the sweep drops a dead run's databases only when the
name says they were made here — and two runs with the same pid in different
namespaces no longer share a name.

Nothing here names a role: whoever runs the suite owns, drops and sweeps (#388).
"""

import asyncio
import os
import re
import subprocess
import warnings
from collections.abc import Awaitable, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

SHAPE = re.compile(r"watcher_rh(?P<ns>\d+)_(?P<pid>\d+)_(?:src|dst)_test")

INSUFFICIENT_PRIVILEGE = "42501"
OBJECT_IN_USE = "55006"
ATTEMPTS = 3
BACKOFF_SECONDS = 0.5

HOLDERS = text(
    "SELECT pid, usename, backend_type, query FROM pg_stat_activity WHERE datname = :name"
)
OWN_SCRATCH = text(
    "SELECT datname FROM pg_database "
    "WHERE datname LIKE 'watcher\\_rh%' AND pg_has_role(datdba, 'USAGE')"
)


class ScratchDatabaseLeaked(UserWarning):
    """A scratch database outlived its run; the next run's sweep retries it."""


def with_database(url: str, name: str) -> str:
    """``url`` pointed at database ``name``, query string kept."""
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", url, count=1)


def pid_namespace() -> int:
    """This process's pid-namespace inode: the scope in which a pid means anything."""
    return os.stat("/proc/self/ns/pid").st_ino


def names(*, pid: int | None = None, ns: int | None = None) -> tuple[str, str]:
    """The source and target scratch database names for one run.

    ``_test``-suffixed, so ``db_safety`` treats them as non-production.
    """
    pid = os.getpid() if pid is None else pid
    ns = pid_namespace() if ns is None else ns
    return (f"watcher_rh{ns}_{pid}_src_test", f"watcher_rh{ns}_{pid}_dst_test")


def is_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process in this pid namespace."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):  # gone, or no pid could be this
        return False
    except PermissionError:
        return True  # someone else's process, but alive
    return True


def dead_pid() -> int:
    """A pid that was alive a moment ago and is not now (a reaped child), for tests."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def stale(
    candidates: Iterable[str],
    *,
    ns: int,
    own_pid: int,
    alive: Callable[[int], bool] = is_alive,
) -> list[str]:
    """The candidates a dead run in this pid namespace left behind, in order."""
    found = []
    for name in candidates:
        match = SHAPE.fullmatch(name)
        if match is None or int(match["ns"]) != ns:
            continue
        pid = int(match["pid"])
        if pid != own_pid and not alive(pid):
            found.append(name)
    return found


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


async def drop(
    conn,
    name: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Drop scratch database ``name`` on an AUTOCOMMIT connection; ``True`` once gone.

    FORCE first; on ``42501`` (a backend this role cannot signal) a plain
    ``DROP``; on ``55006`` from either (still in use) both again, with backoff.
    When every attempt ends in use, warn :class:`ScratchDatabaseLeaked` naming
    the database and what ``pg_stat_activity`` shows on it, and return
    ``False``. Any other error is raised: that is a broken drop, not a race.
    """
    if not SHAPE.fullmatch(name):
        raise ValueError(f"not a scratch database name: {name!r}")
    for attempt in range(ATTEMPTS):
        if attempt:
            await sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        try:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            return True
        except DBAPIError as exc:
            if _sqlstate(exc) == OBJECT_IN_USE:  # signalled, but outlived the ~5 s wait
                last = exc
                continue
            if _sqlstate(exc) != INSUFFICIENT_PRIVILEGE:
                raise
        try:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
            return True
        except DBAPIError as exc:
            if _sqlstate(exc) != OBJECT_IN_USE:
                raise
            last = exc
    holders = (await conn.execute(HOLDERS, {"name": name})).all()
    warnings.warn(
        ScratchDatabaseLeaked(
            f"{name} not dropped after {ATTEMPTS} attempts "
            f"({OBJECT_IN_USE}: {last.orig}); on it (pid, usename, backend_type, query) — "
            f"a NULL usename is a roleless backend such as autovacuum: {holders}"
        ),
        stacklevel=2,
    )
    return False


async def drop_all(conn, scratch: Iterable[str]) -> None:
    """:func:`drop` each name, all of them even when one raises; then raise the first."""
    first: BaseException | None = None
    for name in scratch:
        try:
            await drop(conn, name)
        except Exception as exc:  # noqa: BLE001 — re-raised below, after the rest
            first = first or exc
    if first is not None:
        raise first


async def sweep(
    conn,
    *,
    ns: int | None = None,
    own_pid: int | None = None,
    alive: Callable[[int], bool] = is_alive,
) -> list[str]:
    """Drop the scratch databases dead runs in this pid namespace leaked.

    Only databases this role could drop are considered. Returns the names it
    tried; a drop that still fails warns, as at teardown.
    """
    listed = (await conn.execute(OWN_SCRATCH)).scalars().all()
    targets = stale(
        listed,
        ns=pid_namespace() if ns is None else ns,
        own_pid=os.getpid() if own_pid is None else own_pid,
        alive=alive,
    )
    for name in targets:
        await drop(conn, name)
    return targets
