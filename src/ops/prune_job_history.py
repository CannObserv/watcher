"""One-off: prune the job-history backlog before the hourly task takes over (#296 D7).

``src.workers.retention.prune_job_history`` keeps finished jobs to 7 days
(succeeded) and 30 days (failed, cancelled, aborted). Its first run against the
original backlog — 679 k jobs and 1.83 M events back to March — would have
deleted all of it in one transaction, inside the service's worker. This steps
the horizon down a week at a time instead, from one step below the oldest job,
one statement per slice, from a shell, with progress. Every step still sorts
every event (procrastinate filters outside its sort); what the steps bound is
the rows each transaction deletes:

    source scripts/load-env.sh
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.prune_job_history --dry-run
    WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.prune_job_history

It deletes history irreversibly: take a dump first. Only finished jobs are
touched, so it is safe beside a running worker. The opt-in is the service's own
(``src.core.db_safety``, #233), given for this one command — the dry run
included, since it reads production too — and never in an env file.
"""

import argparse
import asyncio
import math
import os
import sys
import time
from collections.abc import Mapping

import procrastinate

from src.core.db_safety import (
    ALLOW_PRODUCTION_DB_ENV,
    ProductionDatabaseRefused,
    assert_environment_db_allowed,
)
from src.workers.retention import SUCCEEDED_RETENTION_HOURS, apply_retention, backlog_horizons

_OLDEST_HOURS_SQL = (
    "SELECT EXTRACT(EPOCH FROM now() - min(at)) / 3600 AS hours FROM procrastinate_events"
)
_COUNT_SQL = "SELECT count(*) AS n FROM procrastinate_jobs"


def _conninfo(environ: Mapping[str, str]) -> str:
    """The libpq DSN the worker would use — ``src.workers._get_conninfo``'s rule."""
    url = environ.get("PROCRASTINATE_DATABASE_URL") or environ.get("DATABASE_URL", "")
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _prune(conninfo: str, *, step_hours: int, dry_run: bool) -> None:
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo))
    await app.open_async()
    try:
        row = await app.connector.execute_query_one_async(_OLDEST_HOURS_SQL)
        oldest = row["hours"]
        if oldest is None:
            print("no job history — nothing to prune")
            return
        horizons = backlog_horizons(oldest_hours=math.ceil(oldest), step_hours=step_hours)
        before = (await app.connector.execute_query_one_async(_COUNT_SQL))["n"]
        print(f"jobs: {before}; oldest event {oldest / 24:.1f} days ago")
        print("horizons (days): " + ", ".join(f"{h / 24:.1f}" for h in horizons))
        if dry_run:
            print("dry run — nothing deleted")
            return
        for horizon in horizons:
            started = time.monotonic()
            await apply_retention(app.job_manager, horizon_hours=horizon)
            remaining = (await app.connector.execute_query_one_async(_COUNT_SQL))["n"]
            elapsed = time.monotonic() - started
            print(f"  horizon {horizon / 24:6.1f} d: {remaining} jobs remain ({elapsed:.1f}s)")
        print(f"done: {before} → {remaining} jobs; the hourly task holds it from here")
    finally:
        await app.close_async()


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """CLI entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--step-days", type=int, default=7, help="slice width (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    if not environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set — source scripts/load-env.sh", file=sys.stderr)
        return 2
    try:
        assert_environment_db_allowed(environ)
    except ProductionDatabaseRefused as e:
        print(
            f"{e}\n  To prune production deliberately, set {ALLOW_PRODUCTION_DB_ENV}=1 "
            "for this one command.",
            file=sys.stderr,
        )
        return 2
    if args.step_days <= 0:
        print(f"--step-days must be positive, got {args.step_days}", file=sys.stderr)
        return 2

    print(f"policy: succeeded > {SUCCEEDED_RETENTION_HOURS / 24:.0f} days, failed > 30 days")
    asyncio.run(_prune(_conninfo(environ), step_hours=args.step_days * 24, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
