"""Shared test fixtures — async database session and FastAPI TestClient."""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.models import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher_test",
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            nested = await session.begin_nested()
            yield session
            await nested.rollback()


@pytest.fixture
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient]:
    from src.api.dependencies import get_db_session

    from src.api.main import app

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
