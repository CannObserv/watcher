"""Async database engine and session factory."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_DATABASE_URL = "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher"


def get_database_url() -> str:
    """Read database URL from environment or return default."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_async_engine_from_url(url: str):
    """Create an async SQLAlchemy engine."""
    return create_async_engine(url, echo=False)


# Module-level engine and session factory — initialized on import.
engine = create_async_engine_from_url(get_database_url())
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async session. Use as a FastAPI dependency."""
    async with async_session_factory() as session:
        yield session
