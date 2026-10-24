"""Print how many Procrastinate jobs are in ``doing``: the patching-hosts gate (#382).

The knob's ``inflight`` line (``.skills/patching-hosts``) runs this before every
apply step and again inside the reboot chain, as ``exedev`` and never as root.
The skill's gate passes only when stdout is the single word ``0``, so:

- **stdout is the count and nothing else.** No ``configure_logging()``, unlike
  every other entry point: its handler writes to stdout, and one record there
  turns ``0`` into a refusal. Errors go to stderr.
- **Every failure exits non-zero with stdout empty**, which the gate reads as
  "unknown" and refuses on: 2 for a usage or configuration error, 1 for the
  database.
- **The URL comes from the file named by ``--env-file``**, parsed by
  ``scripts/load-env.sh``'s rules and never put on a command line, where any
  process could read it from ``/proc``. ``exedev`` has no Postgres role of its
  own, so peer auth is no way in. The file decides the database: the ambient
  ``DATABASE_URL`` and ``PROCRASTINATE_DATABASE_URL`` are dropped first, so a
  shell with ``load-env.sh`` sourced reads the same database as the chain does.
- **The session is read-only** (``default_transaction_read_only``), and
  production still takes the service's opt-in (``src.core.db_safety``, #233),
  given for this one command as every ``src/ops`` command takes it.

Run it as the knob does::

    cd /home/exedev/watcher && WATCHER_ALLOW_PRODUCTION_DB=1 \\
        .venv/bin/python -m src.ops.inflight --env-file /etc/watcher/.env

The venv's interpreter, not ``uv run``: the knob runs it inside the reboot
chain, where a sync would rewrite the service's environment.
"""

import argparse
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import psycopg

from src.core.db_safety import (
    ALLOW_PRODUCTION_DB_ENV,
    ProductionDatabaseRefused,
    assert_environment_db_allowed,
)
from src.workers import get_conninfo

#: Long enough for a loaded cluster, short of the skill's 60 s command limit.
CONNECT_TIMEOUT_SECONDS = 10

_DOING_SQL = "SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing'"
_URL_KEYS = ("DATABASE_URL", "PROCRASTINATE_DATABASE_URL")
_NAME = re.compile(r"[A-Za-z0-9_]+")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines by ``scripts/load-env.sh``'s rules.

    Blank and ``#`` lines and lines without ``=`` are skipped, an ``export``
    prefix and blanks around the key are dropped, a key that isn't a name is
    skipped, and one pair of matching quotes around the value is stripped.
    Nothing is executed, unlike sourcing the file.
    """
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.rstrip()
        if not _NAME.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def connect_read_only(conninfo: str) -> psycopg.Connection:
    """A connection whose every transaction is read-only."""
    return psycopg.connect(
        conninfo,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options="-c default_transaction_read_only=on",
    )


def count_doing(conninfo: str) -> int:
    """How many Procrastinate jobs are ``doing`` right now."""
    with connect_read_only(conninfo) as conn:
        (count,) = conn.execute(_DOING_SQL).fetchone()
    return count


def _redacted(error: Exception, conninfo: str) -> str:
    """The error's class and first line, with the URL's password masked.

    libpq's messages name the host and the user, and a message built from the
    connection string could carry its password too.
    """
    lines = str(error).strip().splitlines()
    message = f"{type(error).__name__}: {lines[0] if lines else ''}"
    try:
        password = psycopg.conninfo.conninfo_to_dict(conninfo).get("password")
    except psycopg.ProgrammingError:
        password = None
    return message.replace(str(password), "***") if password else message


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ) -> int:
    """CLI entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="the env file holding DATABASE_URL (production: /etc/watcher/.env)",
    )
    args = parser.parse_args(argv)

    try:
        from_file = read_env_file(args.env_file)
    except OSError as e:
        print(f"cannot read {args.env_file}: {e.strerror}", file=sys.stderr)
        return 2
    env = {k: v for k, v in environ.items() if k not in _URL_KEYS}
    env.update({k: v for k, v in from_file.items() if k in _URL_KEYS})
    if not env.get("DATABASE_URL"):
        print(f"no DATABASE_URL in {args.env_file}", file=sys.stderr)
        return 2
    try:
        assert_environment_db_allowed(env)
    except ProductionDatabaseRefused as e:
        print(
            f"{e}\n  To read production deliberately, set {ALLOW_PRODUCTION_DB_ENV}=1 "
            "for this one command.",
            file=sys.stderr,
        )
        return 2
    try:
        conninfo = get_conninfo(env)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    try:
        count = count_doing(conninfo)
    except psycopg.Error as e:
        print(f"cannot count doing jobs: {_redacted(e, conninfo)}", file=sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
