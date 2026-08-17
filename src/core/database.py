"""Async database engine and session factory."""

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.db_safety import database_name
from src.core.logging import get_logger

logger = get_logger(__name__)

#: Credential Alembic connects with (#259). Holds DDL rights on the schema; the
#: application's ``DATABASE_URL`` does not. Optional everywhere: until the
#: operator has run ``scripts/setup-db-roles.sql`` there is only one role, and
#: the fallback below keeps every migration command working unchanged.
MIGRATION_DATABASE_URL_ENV = "WATCHER_MIGRATION_DATABASE_URL"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    """Read database URL from DATABASE_URL environment variable.

    Raises RuntimeError if not set — requires explicit configuration via
    /etc/watcher/.env (production) or repo .env (development).
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. Load env: source scripts/load-env.sh"
        )
    return url


def get_migration_database_url(default: str = "") -> str:
    """Return the URL Alembic should connect with.

    Resolution order (#259):

    1. ``WATCHER_MIGRATION_DATABASE_URL`` — the schema owner's credential.
    2. ``DATABASE_URL`` — the application's. This is the pre-#259 behaviour and
       what every host uses until the two roles exist, which is what makes the
       split shippable ahead of the operator step.
    3. ``default`` — whatever ``alembic.ini`` carries.

    Raises ``RuntimeError`` when none of the three yields a URL. Alembic would
    otherwise be handed ``""`` and fail deep inside engine construction with a
    message that names neither variable.

    Set-but-empty counts as unset throughout: ``EnvironmentFile`` lines and
    shell exports both produce that, and it must not shadow the fallback.
    """
    migration_url = os.environ.get(MIGRATION_DATABASE_URL_ENV) or ""
    app_url = os.environ.get("DATABASE_URL") or ""

    if migration_url and app_url:
        migration_db = database_name(migration_url)
        app_db = database_name(app_url)
        if migration_db != app_db:
            # Legitimate for the scratch-database autogenerate workflow, and
            # also the shape of this change's characteristic mistake: migrating
            # one database while the service serves another. Never silent.
            #
            # The names go in the message, not only in ``extra``: under
            # ``alembic upgrade`` this logger is configured by alembic.ini's
            # fileConfig, whose formatter renders the message alone — an
            # extras-only warning would read as content-free there.
            logger.warning(
                "migration URL names database %r, DATABASE_URL names %r",
                migration_db,
                app_db,
                extra={
                    "migration_database": migration_db,
                    "application_database": app_db,
                },
            )

    if migration_url:
        return migration_url
    if app_url:
        return app_url
    if default:
        return default
    raise RuntimeError(
        f"no migration database URL: set {MIGRATION_DATABASE_URL_ENV} or "
        "DATABASE_URL. Load env: source scripts/load-env.sh"
    )


def get_engine() -> AsyncEngine:
    """Return the shared async engine, creating it on first call."""
    global _engine
    if _engine is None:
        url = get_database_url()
        _engine = create_async_engine(url, echo=False)
        logger.info("database engine created", extra={"url": url.split("@")[-1]})
    return _engine


def reset_engine() -> None:
    """Reset the shared engine and session factory. For testing only."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory
