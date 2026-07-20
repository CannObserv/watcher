"""Production-database startup guard.

Why this exists (#233; ported from archiver's 2026-07-18 incident): the
documented dev-server recipe in AGENTS.md and docs/COMMANDS.md sourced
``/etc/watcher/.env`` and then ran uvicorn directly, so ``DATABASE_URL``
stayed pointed at production. A dev server on 8001 and the live service on
8000 would share one database — and, because the lifespan starts the embedded
Procrastinate worker, the "dev" server would also be a second worker consuming
the production task queue and a second ``DomainRateLimiter`` splitting every
domain's politeness budget.

``scripts/dev_server.sh`` fixes the sanctioned launch path. This module is the
backstop, because a docs-side fix has the same failure mode as the docs bug it
patches: a hand-rolled uvicorn, or a stale recipe copied out of an old plan
doc, still reaches production. The guard lives in the application, so it holds
no matter how the process was started.

The rule is a *positive* assertion, not a comparison against known production
URLs. Comparing URL strings is defeated by cosmetic differences — the same
database is reachable as ``postgresql://…/watcher`` and
``postgresql+asyncpg://…/watcher``, via ``localhost`` or ``127.0.0.1``. The
database *name* is the boundary that actually holds: it must carry a ``_test``
or ``_dev`` suffix, or the caller must opt in explicitly via
``WATCHER_ALLOW_PRODUCTION_DB=1`` — which only ``deploy/watcher.service``
does.
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

#: Suffixes that mark a database as disposable. AGENTS.md already documents the
#: ``_test`` convention for TEST_DATABASE_URL; this enforces it.
NON_PRODUCTION_SUFFIXES = ("_test", "_dev")

#: Env var the systemd unit sets to serve the production database.
ALLOW_PRODUCTION_DB_ENV = "WATCHER_ALLOW_PRODUCTION_DB"

#: Every env var that can point this process at a database. DATABASE_URL feeds
#: the API engine and alembic; PROCRASTINATE_DATABASE_URL overrides it for the
#: embedded worker (``src/workers/__init__.py`` consults it first).
_DB_URL_ENV_VARS = ("DATABASE_URL", "PROCRASTINATE_DATABASE_URL")


class ProductionDatabaseRefused(RuntimeError):
    """Raised when a process would serve production without opting in."""


def database_name(url: str) -> str | None:
    """Return the database name from a SQLAlchemy/libpq URL, or None.

    Returns None when the name cannot be determined, so callers can fail
    closed rather than guess.
    """
    if not url:
        return None
    try:
        # urlsplit handles the credentials, port, and query string, so a
        # password containing an escaped slash cannot be mistaken for a path.
        parts = urlsplit(url)
    except ValueError:
        # urlsplit raises only for a malformed IPv6 literal (an unclosed
        # bracket in the netloc). Every other malformed input comes back with
        # empty parts and is caught by the scheme/netloc check below.
        return None
    # Without a scheme and host this is not a connection URL at all —
    # urlsplit would hand back the whole string as `path`, which would then
    # sail through a naive suffix check.
    if not parts.scheme or not parts.netloc:
        return None
    # A bare host with no path, or a trailing slash, yields an empty name.
    return parts.path.lstrip("/") or None


def is_non_production_database(url: str) -> bool:
    """True when the URL's database name is marked disposable.

    Suffix match, not substring: ``test_watcher`` and ``watcher_testing``
    are production-looking near-misses and must not pass.
    """
    name = database_name(url)
    if name is None:
        return False
    return name.endswith(NON_PRODUCTION_SUFFIXES)


def assert_production_db_allowed(url: str, *, allow_flag: str | None) -> None:
    """Raise ``ProductionDatabaseRefused`` for an un-opted-in production DB.

    ``allow_flag`` is the raw ``WATCHER_ALLOW_PRODUCTION_DB`` value. Only the
    exact string ``"1"`` opts in — a fuzzy truthiness check would let a stray
    value quietly re-open the hole this guard closes.
    """
    if is_non_production_database(url):
        return
    if allow_flag == "1":
        return

    name = database_name(url) or "<unparseable>"
    raise ProductionDatabaseRefused(
        f"refusing to start against database {name!r}: the name carries no "
        f"{' or '.join(NON_PRODUCTION_SUFFIXES)} suffix, so it is treated as "
        "production.\n"
        "  Only the systemd unit (deploy/watcher.service) may serve the "
        f"production database; it sets {ALLOW_PRODUCTION_DB_ENV}=1.\n"
        "  For a dev server use: bash scripts/dev_server.sh\n"
        "  (See #233 and the archiver 2026-07-18 incident note in that script.)"
    )


def assert_environment_db_allowed(environ: Mapping[str, str]) -> None:
    """Gate every database URL the environment supplies.

    Checks ``DATABASE_URL`` unconditionally (its absence fails closed — the
    process could not serve anything real anyway) and
    ``PROCRASTINATE_DATABASE_URL`` when set, because a production value there
    runs the embedded worker against the production queue even when
    ``DATABASE_URL`` points at a test database.
    """
    allow_flag = environ.get(ALLOW_PRODUCTION_DB_ENV)
    assert_production_db_allowed(environ.get("DATABASE_URL", ""), allow_flag=allow_flag)
    worker_url = environ.get("PROCRASTINATE_DATABASE_URL")
    if worker_url:
        assert_production_db_allowed(worker_url, allow_flag=allow_flag)
