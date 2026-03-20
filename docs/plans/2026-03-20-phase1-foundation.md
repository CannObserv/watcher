# Phase 1: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the data layer (PostgreSQL models, migrations), local filesystem storage, Watch CRUD API, and audit logging — the foundation everything else builds on.

**Architecture:** SQLAlchemy 2.0 async ORM with asyncpg driver. Alembic for migrations. FastAPI routes with Pydantic v2 schemas. StorageBackend protocol with LocalStorage implementation. All primary keys are ULIDs.

**Tech Stack:** SQLAlchemy 2.0 (async), asyncpg, Alembic, python-ulid, FastAPI, Pydantic v2, pytest, httpx (TestClient)

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  api/
    main.py              — modify: add router, lifespan
    dependencies.py      — create: DB session dependency
    schemas/
      __init__.py        — create: package init
      watch.py           — create: Pydantic request/response schemas
    routes/
      __init__.py        — create: package init
      watches.py         — create: Watch CRUD endpoints
  core/
    database.py          — create: async engine, session factory
    storage.py           — create: StorageBackend protocol + LocalStorage
    models/
      __init__.py        — create: re-export all models
      base.py            — create: DeclarativeBase, ULIDType, TimestampMixin
      watch.py           — create: Watch model
      audit_log.py       — create: AuditLog model
alembic.ini              — create: Alembic config
alembic/
  env.py                 — create: async migration env
  versions/              — create: migration versions dir
tests/
  conftest.py            — create: shared fixtures (async DB, TestClient)
  core/
    test_storage.py      — create: LocalStorage tests
    test_models.py       — create: model instantiation tests
  api/
    test_watches.py      — create: Watch CRUD endpoint tests
```

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add production and dev dependencies**

Add to `pyproject.toml` `dependencies`:
```
"sqlalchemy[asyncio]>=2.0",
"asyncpg>=0.30.0",
"alembic>=1.15.0",
"python-ulid>=3.0.0",
```

Add to `[dependency-groups]` `dev`:
```
"httpx>=0.28.0",
```

- [ ] **Step 2: Install**

Run: `uv sync`
Expected: resolves and installs all new packages without errors

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "#2 chore: add sqlalchemy, asyncpg, alembic, ulid dependencies"
```

---

## Task 2: SQLAlchemy base and ULID column type

**Files:**
- Create: `src/core/models/__init__.py`
- Create: `src/core/models/base.py`
- Test: `tests/core/test_models.py`

- [ ] **Step 1: Write failing test for ULID type**

Create `tests/core/test_models.py`:

```python
"""Tests for SQLAlchemy base and ULID column type."""

from ulid import ULID

from src.core.models.base import ULIDType


class TestULIDType:
    def test_process_bind_param_converts_ulid_to_string(self):
        ulid_type = ULIDType()
        value = ULID()
        result = ulid_type.process_bind_param(value, dialect=None)
        assert isinstance(result, str)
        assert result == str(value)

    def test_process_bind_param_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_bind_param(None, dialect=None)
        assert result is None

    def test_process_result_value_converts_string_to_ulid(self):
        ulid_type = ULIDType()
        original = ULID()
        result = ulid_type.process_result_value(str(original), dialect=None)
        assert isinstance(result, ULID)
        assert result == original

    def test_process_result_value_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_result_value(None, dialect=None)
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'ULIDType' from 'src.core.models.base'`

- [ ] **Step 3: Create models package init**

Create `src/core/models/__init__.py`:

```python
"""SQLAlchemy models."""
```

- [ ] **Step 4: Implement base module**

Create `src/core/models/base.py`:

```python
"""SQLAlchemy declarative base, ULID primary key type, and shared mixins."""

from datetime import datetime, timezone

from sqlalchemy import String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from ulid import ULID


class ULIDType(TypeDecorator):
    """Store ULIDs as 26-char strings in the database."""

    impl = String(26)
    cache_ok = True

    def process_bind_param(self, value: ULID | None, dialect) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect) -> ULID | None:
        if value is None:
            return None
        return ULID.from_str(value)


def generate_ulid() -> ULID:
    """Generate a new ULID."""
    return ULID()


class Base(DeclarativeBase):
    """Declarative base for all models."""


class TimestampMixin:
    """Mixin adding created_at and updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/core/models/ tests/core/test_models.py
git commit -m "#2 feat: add SQLAlchemy base, ULID column type, TimestampMixin"
```

---

## Task 3: Watch model

**Files:**
- Create: `src/core/models/watch.py`
- Modify: `src/core/models/__init__.py`
- Modify: `tests/core/test_models.py`

- [ ] **Step 1: Write failing test for Watch model**

Append to `tests/core/test_models.py`:

```python
from src.core.models.watch import Watch, ContentType


class TestWatchModel:
    def test_create_watch_with_defaults(self):
        watch = Watch(
            name="Test Watch",
            url="https://example.com/agenda",
            content_type=ContentType.HTML,
        )
        assert watch.name == "Test Watch"
        assert watch.url == "https://example.com/agenda"
        assert watch.content_type == ContentType.HTML
        assert watch.is_active is True
        assert watch.fetch_config == {}
        assert watch.schedule_config == {}

    def test_create_watch_with_all_fields(self):
        watch = Watch(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type=ContentType.PDF,
            fetch_config={"selectors": ["#content"]},
            schedule_config={"interval": "6h"},
            is_active=False,
        )
        assert watch.content_type == ContentType.PDF
        assert watch.fetch_config == {"selectors": ["#content"]}
        assert watch.schedule_config == {"interval": "6h"}
        assert watch.is_active is False

    def test_content_type_enum_values(self):
        assert ContentType.HTML.value == "html"
        assert ContentType.PDF.value == "pdf"
        assert ContentType.FILE.value == "file"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_models.py::TestWatchModel -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement Watch model**

Create `src/core/models/watch.py`:

```python
"""Watch model — a URL to monitor for changes."""

import enum

from sqlalchemy import Boolean, Enum, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class ContentType(str, enum.Enum):
    """Supported content types for monitoring."""

    HTML = "html"
    PDF = "pdf"
    FILE = "file"


class Watch(Base, TimestampMixin):
    """A URL to monitor for changes."""

    __tablename__ = "watches"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[ContentType] = mapped_column(Enum(ContentType, native_enum=False))
    fetch_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    schedule_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
```

- [ ] **Step 4: Update models __init__.py**

Update `src/core/models/__init__.py`:

```python
"""SQLAlchemy models."""

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.core.models.watch import ContentType, Watch

__all__ = ["Base", "ContentType", "TimestampMixin", "ULIDType", "Watch", "generate_ulid"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/core/models/ tests/core/test_models.py
git commit -m "#2 feat: add Watch model with ContentType enum"
```

---

## Task 4: AuditLog model

**Files:**
- Create: `src/core/models/audit_log.py`
- Modify: `src/core/models/__init__.py`
- Modify: `tests/core/test_models.py`

- [ ] **Step 1: Write failing test for AuditLog model**

Append to `tests/core/test_models.py`:

```python
from src.core.models.audit_log import AuditLog


class TestAuditLogModel:
    def test_create_audit_log_entry(self):
        entry = AuditLog(
            event_type="watch.created",
            payload={"watch_name": "Test Watch"},
        )
        assert entry.event_type == "watch.created"
        assert entry.payload == {"watch_name": "Test Watch"}
        assert entry.watch_id is None

    def test_create_audit_log_with_watch_id(self):
        from ulid import ULID

        watch_id = ULID()
        entry = AuditLog(
            event_type="check.started",
            watch_id=watch_id,
            payload={"url": "https://example.com"},
        )
        assert entry.watch_id == watch_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_models.py::TestAuditLogModel -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement AuditLog model**

Create `src/core/models/audit_log.py`:

```python
"""AuditLog model — immutable record of every system operation."""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class AuditLog(Base):
    """Immutable audit log entry."""

    __tablename__ = "audit_log"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    event_type: Mapped[str] = mapped_column(String(100))
    watch_id: Mapped[ULID | None] = mapped_column(
        ULIDType, ForeignKey("watches.id"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
```

- [ ] **Step 4: Update models __init__.py**

Update `src/core/models/__init__.py`:

```python
"""SQLAlchemy models."""

from src.core.models.audit_log import AuditLog
from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.core.models.watch import ContentType, Watch

__all__ = [
    "AuditLog",
    "Base",
    "ContentType",
    "TimestampMixin",
    "ULIDType",
    "Watch",
    "generate_ulid",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add src/core/models/ tests/core/test_models.py
git commit -m "#2 feat: add AuditLog model"
```

---

## Task 5: Database engine and session factory

**Files:**
- Create: `src/core/database.py`
- Modify: `tests/core/test_models.py`

- [ ] **Step 1: Write failing test for database module**

Append to `tests/core/test_models.py`:

```python
from src.core.database import get_database_url, create_async_engine_from_url


class TestDatabase:
    def test_get_database_url_default(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        url = get_database_url()
        assert url == "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher"

    def test_get_database_url_from_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://custom:pass@db:5432/mydb")
        url = get_database_url()
        assert url == "postgresql+asyncpg://custom:pass@db:5432/mydb"

    def test_create_engine_returns_async_engine(self):
        from sqlalchemy.ext.asyncio import AsyncEngine

        engine = create_async_engine_from_url("postgresql+asyncpg://x:x@localhost/test")
        assert isinstance(engine, AsyncEngine)
        # Don't actually connect — just verify engine creation
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_models.py::TestDatabase -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement database module**

Create `src/core/database.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_models.py::TestDatabase -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/core/database.py tests/core/test_models.py
git commit -m "#2 feat: add async database engine and session factory"
```

---

## Task 6: Alembic setup and initial migration

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/` (directory)

- [ ] **Step 1: Initialize Alembic**

Run: `cd /home/exedev/watcher && uv run alembic init alembic`

- [ ] **Step 2: Configure alembic.ini**

Edit `alembic.ini` — set `sqlalchemy.url`:
```ini
sqlalchemy.url = postgresql+asyncpg://watcher:watcher@localhost:5432/watcher
```

- [ ] **Step 3: Configure async env.py**

Replace `alembic/env.py` with async-compatible version:

```python
"""Alembic migration environment — async PostgreSQL."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        config.get_main_option("sqlalchemy.url", "postgresql+asyncpg://watcher:watcher@localhost:5432/watcher"),
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without connecting."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Ensure PostgreSQL is running and create database**

Run:
```bash
psql -U postgres -c "CREATE USER watcher WITH PASSWORD 'watcher';" 2>/dev/null || true
psql -U postgres -c "CREATE DATABASE watcher OWNER watcher;" 2>/dev/null || true
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE watcher TO watcher;"
```

Note: if PostgreSQL is not running, start it first. If credentials differ on this VM, adjust accordingly.

- [ ] **Step 5: Generate initial migration**

Run: `uv run alembic revision --autogenerate -m "create watches and audit_log tables"`
Expected: creates a new migration file in `alembic/versions/`

- [ ] **Step 6: Apply migration**

Run: `uv run alembic upgrade head`
Expected: tables `watches` and `audit_log` created in the database

- [ ] **Step 7: Verify tables exist**

Run: `psql -U watcher -d watcher -c "\dt"`
Expected: `watches` and `audit_log` tables listed

- [ ] **Step 8: Commit**

```bash
git add alembic.ini alembic/
git commit -m "#2 feat: add Alembic setup and initial migration (watches, audit_log)"
```

---

## Task 7: StorageBackend protocol and LocalStorage

**Files:**
- Create: `src/core/storage.py`
- Create: `tests/core/test_storage.py`

- [ ] **Step 1: Write failing tests for LocalStorage**

Create `tests/core/test_storage.py`:

```python
"""Tests for StorageBackend protocol and LocalStorage implementation."""

from pathlib import Path

from src.core.storage import LocalStorage


class TestLocalStorage:
    def test_save_and_load(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        content = b"Hello, world!"
        path = "watches/abc/snap1.html"

        storage.save(path, content)
        result = storage.load(path)

        assert result == content

    def test_save_creates_intermediate_directories(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        path = "deep/nested/dir/file.pdf"

        storage.save(path, b"pdf content")

        assert (tmp_path / "deep" / "nested" / "dir" / "file.pdf").exists()

    def test_load_nonexistent_raises(self, tmp_path):
        import pytest

        storage = LocalStorage(base_dir=tmp_path)

        with pytest.raises(FileNotFoundError):
            storage.load("does/not/exist.txt")

    def test_exists_true(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        storage.save("test.txt", b"data")

        assert storage.exists("test.txt") is True

    def test_exists_false(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)

        assert storage.exists("nope.txt") is False

    def test_build_snapshot_path(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        path = storage.snapshot_path("watch123", "snap456", "pdf")

        assert path == "snapshots/watch123/snap456.pdf"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_storage.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement storage module**

Create `src/core/storage.py`:

```python
"""Storage backend protocol and local filesystem implementation."""

from pathlib import Path
from typing import Protocol


class StorageBackend(Protocol):
    """Protocol for content storage backends."""

    def save(self, path: str, content: bytes) -> None: ...
    def load(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def snapshot_path(self, watch_id: str, snapshot_id: str, extension: str) -> str: ...


class LocalStorage:
    """Store content on the local filesystem."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def save(self, path: str, content: bytes) -> None:
        """Save content to a relative path under base_dir."""
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)

    def load(self, path: str) -> bytes:
        """Load content from a relative path. Raises FileNotFoundError if missing."""
        full_path = self.base_dir / path
        return full_path.read_bytes()

    def exists(self, path: str) -> bool:
        """Check if a path exists."""
        return (self.base_dir / path).is_file()

    def snapshot_path(self, watch_id: str, snapshot_id: str, extension: str) -> str:
        """Build the conventional storage path for a snapshot."""
        return f"snapshots/{watch_id}/{snapshot_id}.{extension}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_storage.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/core/storage.py tests/core/test_storage.py
git commit -m "#2 feat: add StorageBackend protocol and LocalStorage implementation"
```

---

## Task 8: Test fixtures (conftest.py)

**Files:**
- Create: `tests/conftest.py`

This sets up the shared fixtures needed by integration tests: async database session and FastAPI TestClient. Unit tests (Tasks 2–7) don't need these, but the API tests in Tasks 9–11 do.

- [ ] **Step 1: Create conftest.py with async DB fixtures**

Create `tests/conftest.py`:

```python
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
```

- [ ] **Step 2: Create the test database**

Run:
```bash
psql -U postgres -c "CREATE DATABASE watcher_test OWNER watcher;" 2>/dev/null || true
```

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "#2 test: add shared async DB and TestClient fixtures"
```

---

## Task 9: Pydantic schemas for Watch

**Files:**
- Create: `src/api/schemas/__init__.py`
- Create: `src/api/schemas/watch.py`
- Create: `tests/api/test_schemas.py`

- [ ] **Step 1: Write failing tests for Watch schemas**

Create `tests/api/test_schemas.py`:

```python
"""Tests for Watch Pydantic schemas."""

import pytest
from pydantic import ValidationError

from src.api.schemas.watch import WatchCreate, WatchUpdate


class TestWatchCreate:
    def test_valid_watch_create(self):
        data = WatchCreate(
            name="Test Watch",
            url="https://example.com/page",
            content_type="html",
        )
        assert data.name == "Test Watch"
        assert data.url == "https://example.com/page"
        assert data.content_type == "html"
        assert data.fetch_config == {}
        assert data.schedule_config == {}

    def test_watch_create_requires_name(self):
        with pytest.raises(ValidationError):
            WatchCreate(url="https://example.com", content_type="html")

    def test_watch_create_requires_url(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", content_type="html")

    def test_watch_create_validates_content_type(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", url="https://example.com", content_type="invalid")

    def test_watch_create_with_configs(self):
        data = WatchCreate(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type="pdf",
            fetch_config={"timeout": 30},
            schedule_config={"interval": "6h"},
        )
        assert data.fetch_config == {"timeout": 30}
        assert data.schedule_config == {"interval": "6h"}


class TestWatchUpdate:
    def test_update_partial(self):
        data = WatchUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.url is None
        assert data.is_active is None

    def test_update_all_fields(self):
        data = WatchUpdate(
            name="Updated",
            url="https://new.example.com",
            content_type="pdf",
            fetch_config={"selectors": ["#main"]},
            schedule_config={"interval": "1h"},
            is_active=False,
        )
        assert data.is_active is False

    def test_update_empty_is_valid(self):
        data = WatchUpdate()
        assert data.name is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_schemas.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Create schemas package**

Create `src/api/schemas/__init__.py`:

```python
"""Pydantic schemas for API request/response validation."""
```

- [ ] **Step 4: Implement Watch schemas**

Create `src/api/schemas/watch.py`:

```python
"""Pydantic schemas for Watch CRUD operations."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.core.models.watch import ContentType


class WatchCreate(BaseModel):
    """Schema for creating a new watch."""

    name: str
    url: str
    content_type: ContentType
    fetch_config: dict = {}
    schedule_config: dict = {}


class WatchUpdate(BaseModel):
    """Schema for updating a watch. All fields optional."""

    name: str | None = None
    url: str | None = None
    content_type: ContentType | None = None
    fetch_config: dict | None = None
    schedule_config: dict | None = None
    is_active: bool | None = None


class WatchResponse(BaseModel):
    """Schema for returning a watch."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    url: str
    content_type: ContentType
    fetch_config: dict
    schedule_config: dict
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_schemas.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add src/api/schemas/ tests/api/test_schemas.py
git commit -m "#2 feat: add Pydantic schemas for Watch CRUD"
```

---

## Task 10: FastAPI dependencies and Watch CRUD routes

**Files:**
- Create: `src/api/dependencies.py`
- Create: `src/api/routes/__init__.py`
- Create: `src/api/routes/watches.py`
- Modify: `src/api/main.py`
- Create: `tests/api/test_watches.py`

- [ ] **Step 1: Write failing integration tests for Watch CRUD**

Create `tests/api/test_watches.py`:

```python
"""Integration tests for Watch CRUD API endpoints."""

import pytest


pytestmark = pytest.mark.integration


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client):
        response = await client.post("/api/watches", json={
            "name": "Test Watch",
            "url": "https://example.com/page",
            "content_type": "html",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["url"] == "https://example.com/page"
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data

    async def test_create_watch_with_config(self, client):
        response = await client.post("/api/watches", json={
            "name": "PDF Watch",
            "url": "https://example.com/report.pdf",
            "content_type": "pdf",
            "fetch_config": {"timeout": 30},
            "schedule_config": {"interval": "6h"},
        })
        assert response.status_code == 201
        data = response.json()
        assert data["fetch_config"] == {"timeout": 30}

    async def test_create_watch_invalid_content_type(self, client):
        response = await client.post("/api/watches", json={
            "name": "Bad",
            "url": "https://example.com",
            "content_type": "invalid",
        })
        assert response.status_code == 422


class TestListWatches:
    async def test_list_watches_empty(self, client):
        response = await client.get("/api/watches")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_watches_returns_created(self, client):
        await client.post("/api/watches", json={
            "name": "Watch 1",
            "url": "https://example.com/1",
            "content_type": "html",
        })
        response = await client.get("/api/watches")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Watch 1"


class TestGetWatch:
    async def test_get_watch_by_id(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Get Me",
            "url": "https://example.com/get",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Original",
            "url": "https://example.com/orig",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.patch(f"/api/watches/{watch_id}", json={
            "name": "Updated",
        })
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"
        assert response.json()["url"] == "https://example.com/orig"

    async def test_update_watch_not_found(self, client):
        response = await client.patch(
            "/api/watches/00000000000000000000000000",
            json={"name": "Nope"},
        )
        assert response.status_code == 404


class TestDeactivateWatch:
    async def test_deactivate_watch(self, client):
        create_resp = await client.post("/api/watches", json={
            "name": "Deactivate Me",
            "url": "https://example.com/deact",
            "content_type": "html",
        })
        watch_id = create_resp.json()["id"]

        response = await client.post(f"/api/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_deactivate_watch_not_found(self, client):
        response = await client.post("/api/watches/00000000000000000000000000/deactivate")
        assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_watches.py -v -m integration`
Expected: FAIL — `ImportError` (dependencies module doesn't exist yet)

- [ ] **Step 3: Create dependencies module**

Create `src/api/dependencies.py`:

```python
"""FastAPI dependencies — database session injection."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import async_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with async_session_factory() as session:
        yield session
```

- [ ] **Step 4: Create routes package and Watch CRUD routes**

Create `src/api/routes/__init__.py`:

```python
"""API route modules."""
```

Create `src/api/routes/watches.py`:

```python
"""Watch CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.models.audit_log import AuditLog
from src.core.models.watch import Watch

router = APIRouter(prefix="/api/watches", tags=["watches"])


@router.post("", status_code=201, response_model=WatchResponse)
async def create_watch(
    data: WatchCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new watch."""
    watch = Watch(
        name=data.name,
        url=data.url,
        content_type=data.content_type,
        fetch_config=data.fetch_config,
        schedule_config=data.schedule_config,
    )
    session.add(watch)
    audit = AuditLog(
        event_type="watch.created",
        watch_id=watch.id,
        payload={"name": data.name, "url": data.url, "content_type": data.content_type.value},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.get("", response_model=list[WatchResponse])
async def list_watches(
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """List all watches, optionally filtered by active status."""
    stmt = select(Watch).order_by(Watch.created_at.desc())
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/{watch_id}", response_model=WatchResponse)
async def get_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Get a watch by ID."""
    watch = await session.get(Watch, ULID.from_str(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    return watch


@router.patch("/{watch_id}", response_model=WatchResponse)
async def update_watch(
    watch_id: str,
    data: WatchUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    """Update a watch. Only provided fields are changed."""
    watch = await session.get(Watch, ULID.from_str(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(watch, field, value)

    audit = AuditLog(
        event_type="watch.updated",
        watch_id=watch.id,
        payload={"updated_fields": list(updates.keys())},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.post("/{watch_id}/deactivate", response_model=WatchResponse)
async def deactivate_watch(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch without deleting it."""
    watch = await session.get(Watch, ULID.from_str(watch_id))
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    watch.is_active = False
    audit = AuditLog(
        event_type="watch.deactivated",
        watch_id=watch.id,
        payload={"name": watch.name},
    )
    session.add(audit)
    await session.commit()
    await session.refresh(watch)
    return watch
```

- [ ] **Step 5: Update main.py to include router**

Update `src/api/main.py`:

```python
"""FastAPI application entry point."""

from fastapi import FastAPI

from src.api.routes.watches import router as watches_router
from src.core.logging import configure_logging

configure_logging()

app = FastAPI(title="watcher", version="0.1.0")
app.include_router(watches_router)
```

- [ ] **Step 6: Run integration tests**

Run: `uv run pytest tests/api/test_watches.py -v -m integration`
Expected: 10 passed

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest -v`
Expected: all unit tests pass (integration tests excluded by default)

- [ ] **Step 8: Run linter**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add src/api/ tests/api/
git commit -m "#2 feat: add Watch CRUD API with audit logging"
```

---

## Task 11: Verify end-to-end

- [ ] **Step 1: Start the dev server**

Run: `uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000`

- [ ] **Step 2: Test API manually**

In another terminal:

```bash
# Create a watch
curl -s -X POST http://localhost:8000/api/watches \
  -H "Content-Type: application/json" \
  -d '{"name":"WA Cannabis Board","url":"https://app.leg.wa.gov/committeeschedules/Home/Agenda/33957","content_type":"html"}' | python -m json.tool

# List watches
curl -s http://localhost:8000/api/watches | python -m json.tool

# Check OpenAPI docs load
curl -s http://localhost:8000/openapi.json | python -m json.tool | head -20
```

- [ ] **Step 3: Verify audit log was written**

```bash
psql -U watcher -d watcher -c "SELECT event_type, payload FROM audit_log ORDER BY created_at DESC LIMIT 5;"
```

- [ ] **Step 4: Stop the dev server and commit any fixes**

If any issues were found and fixed, commit them:

```bash
git add -A
git commit -m "#2 fix: address issues found in end-to-end verification"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Dependencies | — |
| 2 | ULID type, Base, TimestampMixin | 4 unit |
| 3 | Watch model | 3 unit |
| 4 | AuditLog model | 2 unit |
| 5 | Database engine/session | 3 unit |
| 6 | Alembic + migration | manual verify |
| 7 | StorageBackend + LocalStorage | 6 unit |
| 8 | Test fixtures (conftest) | — |
| 9 | Pydantic schemas | 8 unit |
| 10 | Watch CRUD routes + audit log | 10 integration |
| 11 | End-to-end verification | manual |

Total: ~36 automated tests (26 unit + 10 integration)
