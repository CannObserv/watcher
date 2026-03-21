"""Procrastinate task queue — app setup and worker configuration.

Uses lazy initialization to avoid import-time side effects.
Call get_app() to get the configured App instance.
"""

import os

import procrastinate

from src.core.logging import get_logger

logger = get_logger(__name__)

_app: procrastinate.App | None = None

# Blueprint for task registration — tasks register against this, not the App.
# Avoids circular imports since tasks.py can import bp without triggering App creation.
bp = procrastinate.Blueprint()


def _get_conninfo() -> str:
    """Get libpq-style connection string for procrastinate."""
    url = os.environ.get("PROCRASTINATE_DATABASE_URL")
    if url:
        return url
    sa_url = os.environ.get("DATABASE_URL", "")
    if sa_url.startswith("postgresql+asyncpg://"):
        return sa_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if sa_url.startswith("postgresql://"):
        return sa_url
    raise RuntimeError(
        "PROCRASTINATE_DATABASE_URL or DATABASE_URL environment variable is not set."
    )


def get_app() -> procrastinate.App:
    """Return the procrastinate App, creating it on first call."""
    global _app
    if _app is None:
        _app = procrastinate.App(
            connector=procrastinate.PsycopgConnector(conninfo=_get_conninfo()),
            import_paths=["src.workers.tasks"],
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
