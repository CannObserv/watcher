"""Information service async engine + session factory.

Reads the database URL from INFORMATION_DATABASE_URL, falling back to
DATABASE_URL for prototype convenience (single-instance Postgres on this VM).
"""

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.information.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    url = os.environ.get("INFORMATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Neither INFORMATION_DATABASE_URL nor DATABASE_URL is set. "
            "Load env: export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)"
        )
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_database_url()
        _engine = create_async_engine(url, echo=False)
        logger.info("information db engine created", extra={"url": url.split("@")[-1]})
    return _engine


def reset_engine() -> None:
    """Test-only: clear cached engine + factory."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory
