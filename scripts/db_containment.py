"""Refuse a non-production connection whose role can reach production (#388).

``src.core.db_safety`` checks the database *name* a URL carries. That is the
boundary for which database a process serves, but it says nothing about the
credential: until #388 ``TEST_DATABASE_URL`` logged in as ``watcher``, the
production schema owner, so the only thing between pytest's ``DROP SCHEMA …
CASCADE`` and production was a URL nobody misedited and a name check nobody
regressed.

This asks the cluster instead: which databases outside the disposable set can
the connected role **connect to, or act as the owner of**? Ownership counts on
its own because an owner can grant itself ``CONNECT`` back. Any such database
fails the check. The maintenance databases — ``postgres``, ``template0``,
``template1`` — are exempt by name: ``postgres`` grants PUBLIC ``CONNECT`` by
default, and CI's container runs its suite as the superuser, which has every
privilege on every database there.

Two callers: ``tests/conftest.py`` before the suite touches the test database,
and ``scripts/dev_server.sh`` (``python -m scripts.db_containment``) before it
resets or migrates the dev database. Neither is runtime code.
"""

import asyncio
import os
import sys
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from src.core.db_safety import NON_PRODUCTION_SUFFIXES

#: Present on every cluster, owned by the bootstrap superuser, never watcher's.
EXEMPT_DATABASES = ("postgres", "template0", "template1")

_REACHABLE = text(
    """
    SELECT datname::text FROM pg_database
     WHERE datname::text <> ALL (CAST(:exempt AS text[]))
       AND NOT EXISTS (
             SELECT 1 FROM unnest(CAST(:suffixes AS text[])) AS s(suffix)
              WHERE right(datname::text, length(s.suffix)) = s.suffix)
       AND (has_database_privilege(oid, 'CONNECT') OR pg_has_role(datdba, 'USAGE'))
     ORDER BY datname
    """
)


class RoleReachesProduction(RuntimeError):
    """The connected role can reach a database that is not disposable."""


async def reachable_production_databases(
    conn: AsyncConnection,
    *,
    suffixes: Sequence[str] = NON_PRODUCTION_SUFFIXES,
    exempt: Sequence[str] = EXEMPT_DATABASES,
) -> list[str]:
    """The non-disposable databases the connected role can connect to or owns."""
    result = await conn.execute(_REACHABLE, {"exempt": list(exempt), "suffixes": list(suffixes)})
    return list(result.scalars())


async def assert_contained(conn: AsyncConnection, **kwargs) -> None:
    """Raise :class:`RoleReachesProduction` naming the role and the databases."""
    reached = await reachable_production_databases(conn, **kwargs)
    if reached:
        role = (await conn.execute(text("SELECT current_user"))).scalar()
        raise RoleReachesProduction(
            f"role {role!r} can connect to or owns {reached}: refusing to run destructive "
            "DDL as a role that reaches production. Log in as the test role "
            "(watcher_tester), which reaches only *_test and *_dev databases — "
            "docs/COMMANDS.md -> 'The test database' (#388)."
        )


async def assert_url_contained(url: str) -> None:
    """Connect to ``url`` and :func:`assert_contained`. Any driver spelling works."""
    engine = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"))
    try:
        async with engine.connect() as conn:
            await assert_contained(conn)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 when the role is contained, 1 when it reaches production, 2 on usage.

    The URL is the first argument, else ``DATABASE_URL``. It is never printed:
    it carries a password, and this output lands in terminals and issues.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    url = args[0] if args else os.environ.get("DATABASE_URL", "")
    if not url:
        print("db_containment: no URL given and DATABASE_URL is unset", file=sys.stderr)
        return 2
    try:
        asyncio.run(assert_url_contained(url))
    except RoleReachesProduction as refused:
        print(f"db_containment: {refused}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
