"""FastAPI dependencies — database session injection."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import async_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with async_session_factory() as session:
        yield session
