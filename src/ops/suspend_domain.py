"""Break-glass: suspend a host, or resume it (#372).

Stops Watcher checking every item on one host, from a shell, with Archiver down:

    source scripts/load-env.sh
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.suspend_domain <host>
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.suspend_domain <host> --resume

It sets ``Domain.is_active`` and every item's ``domain_suspended`` through
``set_domain_active`` — the dashboard toggle's own service — and audits the
change with ``source="cli"``. It is the one operator control Watcher keeps
(charter rule 2): host-local, so it touches the database and nothing else. No
bus, no worker, no Archiver; a test pins that import set (#262).

The running service acts on it: ``schedule_tick`` stops enqueuing the host's
items and an already-queued check no-ops; a ``content.fetch`` command issued
before the suspension still goes out. Since F1 (#374) nothing downstream sees
it: Replicator takes host policy from Archiver. A suspension survives re-announcement: the registry
reconcile re-derives the domain only when an item's host moves.

Exit 0 on a change or a no-op (it says which), 1 for an unknown or archived
host, 2 when it could not run: a configuration or usage error, or a database it
could not use. The opt-in is the service's own (``src.core.db_safety``, #233),
given for this one command, never in an env file.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from typing import NamedTuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.db_safety import (
    ALLOW_PRODUCTION_DB_ENV,
    ProductionDatabaseRefused,
    assert_environment_db_allowed,
)
from src.core.domain_suspension import DomainArchived, DomainNotFound, set_domain_active
from src.core.logging import configure_logging

AUDIT_SOURCE = "cli"


class Outcome(NamedTuple):
    """Exit status and the line to print (stdout when 0, stderr otherwise)."""

    code: int
    message: str


def _host(value: str) -> str:
    """argparse type: a bare hostname, lowercased as ``Domain.name`` is stored."""
    host = value.strip().lower()
    if not host or "/" in host:
        raise argparse.ArgumentTypeError(
            f"expected a host such as lcb.wa.gov, not {value!r} (no scheme, no path)"
        )
    return host


def _items(n: int) -> str:
    return f"{n} item{'' if n == 1 else 's'}"


async def apply(session: AsyncSession, host: str, *, resume: bool) -> Outcome:
    """Suspend (or resume) ``host`` in ``session`` and say what happened. Does not commit."""
    try:
        result = await set_domain_active(session, host, active=resume, source=AUDIT_SOURCE)
    except DomainNotFound:
        return Outcome(
            1, f"no domain named {host!r} — nothing to {'resume' if resume else 'suspend'}"
        )
    except DomainArchived:
        # Archiving revokes the host's fetch policy but flags no item, so its
        # live items are still scheduled: say how to stop them (CR 1).
        return Outcome(
            1,
            f"{host} is archived: suspension does not apply to an archived domain, and "
            "archiving does not stop its items — restore it, then suspend it",
        )
    if result.changed:
        verb = "resumed" if resume else "suspended"
        return Outcome(0, f"{verb} {host}: {_items(result.items_updated)} updated")
    message = f"{host} is {'not' if resume else 'already'} suspended — nothing to do"
    if result.items_updated:
        message += f" (re-flagged {_items(result.items_updated)} that disagreed)"
    return Outcome(0, message)


async def _run(url: str, host: str, *, resume: bool) -> Outcome:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            outcome = await apply(session, host, resume=resume)
            if outcome.code == 0:
                await session.commit()
            return outcome
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """CLI entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", type=_host, help="the domain's host name, e.g. lcb.wa.gov")
    parser.add_argument("--resume", action="store_true", help="clear the suspension instead")
    args = parser.parse_args(argv)

    configure_logging()
    url = environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set — source scripts/load-env.sh", file=sys.stderr)
        return 2
    try:
        assert_environment_db_allowed(environ)
    except ProductionDatabaseRefused as e:
        print(
            f"{e}\n  To change production deliberately, set {ALLOW_PRODUCTION_DB_ENV}=1 "
            "for this one command.",
            file=sys.stderr,
        )
        return 2

    try:
        outcome = asyncio.run(_run(url, args.host, resume=args.resume))
    except (SQLAlchemyError, OSError) as e:
        # Not 1: that means the host was refused, and a runbook must be able to
        # tell refused from never ran (CR 2). The first line only — SQLAlchemy
        # appends the statement and its parameters — and the type always, since
        # a connect timeout carries no message at all (CR 5).
        detail = next(iter(str(e).splitlines()), "")
        reason = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
        print(f"could not use the database: {reason}", file=sys.stderr)
        return 2
    print(outcome.message, file=sys.stdout if outcome.code == 0 else sys.stderr)
    return outcome.code


if __name__ == "__main__":
    sys.exit(main())
