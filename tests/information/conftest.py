"""Shared fixtures for Information service tests — async engine + httpx client."""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.information.api.deps import get_db_session
from src.information.api.main import app
from src.information.core.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL is not set. "
        "Load env: export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)"
    )


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Ensure the information schema exists before create_all binds tables to it.
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS information"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP SCHEMA IF EXISTS information CASCADE"))
    await engine.dispose()


@pytest.fixture
async def session(test_engine) -> AsyncGenerator[AsyncSession]:
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def client(test_engine, session) -> AsyncGenerator[AsyncClient]:
    async def _override_session():
        yield session

    app.dependency_overrides[get_db_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
