"""Procrastinate task queue — app setup and worker configuration.

Uses lazy initialization to avoid import-time side effects.
Call get_app() to get the configured App instance.
"""

import os
from collections.abc import Mapping

import procrastinate

from src.core.logging import get_logger

logger = get_logger(__name__)

_app: procrastinate.App | None = None

# Blueprint for task registration — tasks register against this, not the App.
# Avoids circular imports since tasks.py can import bp without triggering App creation.
bp = procrastinate.Blueprint()


def get_conninfo(environ: Mapping[str, str] = os.environ) -> str:
    """Get libpq-style connection string for procrastinate.

    Public, and taking ``environ``, because the backlog prune
    (``src.ops.prune_job_history``) must connect exactly where the worker
    would — one rule, not a copy of it (#296 CR 20).
    """
    url = environ.get("PROCRASTINATE_DATABASE_URL")
    if url:
        return url
    sa_url = environ.get("DATABASE_URL", "")
    if sa_url.startswith("postgresql+asyncpg://"):
        return sa_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if sa_url.startswith("postgresql://"):
        return sa_url
    raise RuntimeError(
        "PROCRASTINATE_DATABASE_URL, or a postgresql:// or postgresql+asyncpg:// "
        "DATABASE_URL, is required."
    )


def get_app() -> procrastinate.App:
    """Return the procrastinate App, creating it on first call."""
    global _app
    if _app is None:
        # Import task modules so decorators register on the blueprint
        # before we copy tasks into the App.
        import src.workers.fetch_commands  # noqa: F401
        import src.workers.fetch_policy  # noqa: F401
        import src.workers.retention  # noqa: F401
        import src.workers.source_revisions_drain  # noqa: F401
        import src.workers.tasks  # noqa: F401
        import src.workers.watch_status  # noqa: F401

        _app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=get_conninfo()),
        )
        _app.add_tasks_from(bp, namespace="")
        logger.info("procrastinate app created")
    return _app


def reset_app() -> None:
    """Reset the App singleton. For testing only."""
    global _app
    _app = None


def __getattr__(name: str):
    """Lazy CLI alias — `procrastinate --app=src.workers.app` resolves here."""
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
