# Information Service Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a minimal FastAPI **Information service** prototype on port 8020 that owns the canonical registry of Information Items + Information Source Specifications (InfoSpecs), with priority-ordered InfoSpecs and a JSON-Schema-validated immutable document body.

**Architecture:** Sibling FastAPI app under `src/information/` with its own SQLAlchemy `Base`, async engine, alembic migrations, and systemd unit. Postgres-backed, separate schema in the existing instance. Mirrors the Notifier extraction pattern (`/home/exedev/notifier/`) so later relocation to a sibling repo is mechanical. Single-shared-secret bearer auth via `X-API-Key` header for prototype; real multi-tenant `api_keys` table is deferred.

**Tech Stack:** FastAPI, SQLAlchemy 2 async + asyncpg, Pydantic v2, Alembic, `python-ulid`, `jsonschema`, pytest + httpx ASGITransport.

**Reference:** Design doc at `docs/plans/2026-05-03-information-source-specifications-design.md` (issue #138).

---

## Pre-flight

- This plan executes from the worktree at `/home/exedev/watcher/.worktrees/feat-138-information-service-phase1` on branch `feat/138-information-service-phase1`.
- Tests use the existing `TEST_DATABASE_URL` (loaded from `/etc/watcher/.env` + `.env`).
- All terminal commands assume env loaded: `export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)`.
- Long-form prose: "Information Item", "Information Source Specification". Code: `info_item`, `info_spec`. Casual prose: "InfoSpec".
- Nothing in this plan modifies Watcher's existing `src/api/`, `src/core/models/`, or `alembic/`. The Information service is fully additive.

## File Structure

**Created:**
```
src/information/
  __init__.py
  api/
    __init__.py
    main.py
    deps.py
    routes/
      __init__.py
      health.py
      info_items.py
      info_specs.py
    schemas/
      __init__.py
      info_item.py
      info_spec.py
  core/
    __init__.py
    database.py
    logging.py
    models/
      __init__.py
      base.py
      info_item.py
      info_spec.py
    info_spec_schema/
      __init__.py
      v1.json
      validator.py
alembic_information/                # Information service migrations (own root)
  env.py
  script.py.mako
  versions/
    <hash>_initial_information_schema.py
alembic_information.ini
deploy/
  information.service               # New systemd unit
tests/information/
  __init__.py
  conftest.py
  api/
    __init__.py
    test_health.py
    test_info_items.py
    test_info_specs_create.py
    test_info_specs_list.py
    test_primary_info_spec.py
    test_info_specs_patch.py
    test_auth.py
    test_smoke_e2e.py
  core/
    __init__.py
    test_models.py
    test_validator.py
```

**Modified:**
- `pyproject.toml:?` — add `jsonschema` (Watcher already has FastAPI, SQLAlchemy, alembic, python-ulid, asyncpg)
- `AGENTS.md:?` — add Information service to Infrastructure / Server Lifecycle sections (port 8020)
- `docs/COMMANDS.md:?` — add Information service alembic command

**Why a separate alembic root (`alembic_information/`) and `alembic_information.ini`?** Watcher's existing `alembic/` and `alembic.ini` at repo root manage Watcher's tables. Adding the Information service's migrations to that same root would mix two services' lineages. Per Notifier's precedent (own `alembic/`), Information gets a parallel root. Both invocations remain `uv run alembic -c <ini> upgrade head`. When the Information service later extracts to a sibling repo, the alembic_information directory rebases to the new repo's root.

---

## Task 1: Scaffold Information service skeleton + health endpoint

**Files:**
- Create: `src/information/__init__.py`
- Create: `src/information/api/__init__.py`
- Create: `src/information/api/main.py`
- Create: `src/information/api/routes/__init__.py`
- Create: `src/information/api/routes/health.py`
- Create: `src/information/core/__init__.py`
- Create: `src/information/core/logging.py`
- Create: `tests/information/__init__.py`
- Create: `tests/information/api/__init__.py`
- Create: `tests/information/api/test_health.py`

- [ ] **Step 1: Write the failing health-endpoint test**

`tests/information/api/test_health.py`:
```python
"""Health endpoint smoke test."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.information.api.main import app


@pytest.mark.asyncio
async def test_health_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/information/api/test_health.py -v --no-cov
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.information'`.

- [ ] **Step 3: Create empty package init files**

```bash
mkdir -p src/information/api/routes src/information/core
touch src/information/__init__.py
touch src/information/api/__init__.py
touch src/information/api/routes/__init__.py
touch src/information/core/__init__.py
```

- [ ] **Step 4: Create `src/information/core/logging.py`**

```python
"""Logging adapter — delegates to the shared watcher logging module.

When the Information service extracts to its own repo, this module becomes
its own logging configuration.
"""

from src.core.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
```

- [ ] **Step 5: Create the health route**

`src/information/api/routes/health.py`:
```python
"""Liveness endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — returns ok if the process is up."""
    return {"status": "ok"}
```

- [ ] **Step 6: Create `src/information/api/main.py`**

```python
"""Information service — FastAPI application entry point."""

from fastapi import FastAPI

from src.information.api.routes.health import router as health_router
from src.information.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="information", version="0.1.0")
app.include_router(health_router)
```

- [ ] **Step 7: Run test to verify it passes**

```bash
uv run pytest tests/information/api/test_health.py -v --no-cov
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/information tests/information
git commit -m "#138 feat: scaffold Information service skeleton + /health"
```

---

## Task 2: Database engine, base model, ULID type, alembic root, deps stub

**Files:**
- Create: `src/information/core/database.py`
- Create: `src/information/core/models/__init__.py`
- Create: `src/information/core/models/base.py`
- Create: `src/information/api/deps.py` (stub — only `get_db_session`; auth lands in Task 4)
- Create: `alembic_information.ini`
- Create: `alembic_information/env.py`
- Create: `alembic_information/script.py.mako`
- Create: `alembic_information/versions/.gitkeep`

**Schema separation (per spec default):** Information's tables live in a Postgres `information` schema. The first migration (Task 3) creates the schema; models declare `__table_args__ = {"schema": "information"}`; alembic env uses `include_schemas=True` + `version_table_schema="information"` plus an `include_object` filter so autogenerate ignores Watcher's tables in `public`. **This is critical** — without the filter, autogenerate would emit `op.drop_table(...)` for every Watcher table on the very first revision.

- [ ] **Step 1: Create `src/information/core/models/base.py`**

This mirrors `src/core/models/base.py` but uses an INDEPENDENT `Base` so Information's metadata is separate from Watcher's.

```python
"""Information service SQLAlchemy declarative base + ULID type."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from ulid import ULID


class ULIDType(TypeDecorator):
    """Store ULIDs as 26-char strings."""

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
    return ULID()


class Base(DeclarativeBase):
    """Information service declarative base — distinct from watcher's Base."""


class TimestampMixin:
    """Adds created_at / updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )
```

- [ ] **Step 2: Create `src/information/core/models/__init__.py`**

```python
"""Information service ORM models."""

from src.information.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid

__all__ = ["Base", "TimestampMixin", "ULIDType", "generate_ulid"]
```

- [ ] **Step 3: Create `src/information/core/database.py`**

```python
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
```

- [ ] **Step 4: Create `alembic_information.ini`**

Mirror Watcher's `alembic.ini` with `script_location = alembic_information` and the same `sqlalchemy.url = ` empty value (env-driven via `env.py`). Copy these top-level keys from Watcher's `alembic.ini` and adjust:

```ini
[alembic]
script_location = alembic_information
prepend_sys_path = .
version_path_separator = os
sqlalchemy.url =

[post_write_hooks]

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 5: Create `alembic_information/env.py`**

This env adds three things on top of Watcher's pattern: an `include_object` filter that restricts autogenerate to the `information` schema, `include_schemas=True` so cross-schema diffing works, and `version_table_schema="information"` so the alembic version table also lives in `information`.

```python
"""Alembic environment for the Information service.

Tables are scoped to a Postgres `information` schema; this env filters
autogenerate so it only sees objects in that schema (and ignores Watcher's
tables in `public`).
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.information.core.models import Base
from src.information.core.models.base import ULIDType

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

INFORMATION_SCHEMA = "information"


def include_object(object_, name, type_, reflected, compare_to):
    """Restrict autogenerate to objects in the `information` schema.

    Without this, alembic would compare Information's metadata against the
    full live database (including Watcher's `public`-schema tables) and emit
    spurious drop_table operations.
    """
    if type_ == "table":
        return getattr(object_, "schema", None) == INFORMATION_SCHEMA
    if type_ in ("index", "unique_constraint", "foreign_key_constraint"):
        table = getattr(object_, "table", None)
        if table is None:
            return True
        return getattr(table, "schema", None) == INFORMATION_SCHEMA
    return True


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, ULIDType):
        return "sa.String(length=26)"
    return False


def get_url() -> str:
    return os.environ.get(
        "INFORMATION_DATABASE_URL",
        os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url", "")),
    )


def _common_configure_kwargs() -> dict:
    return dict(
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=True,
        version_table_schema=INFORMATION_SCHEMA,
        render_item=render_item,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, **_common_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 5b: Create `src/information/api/deps.py` stub**

Only `get_db_session` lands here in Task 2; `require_api_key` is added in Task 4. Doing this now means the conftest in Task 3 can import `get_db_session` cleanly.

```python
"""FastAPI dependencies for the Information service."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.information.core.database import get_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session
```

- [ ] **Step 6: Copy `alembic/script.py.mako` to `alembic_information/script.py.mako`**

```bash
cp alembic/script.py.mako alembic_information/script.py.mako
mkdir -p alembic_information/versions
touch alembic_information/versions/.gitkeep
```

- [ ] **Step 7: Sanity-check alembic discovers the env**

```bash
uv run alembic -c alembic_information.ini current
```

Expected: prints empty current revision (no migrations yet) without errors.

- [ ] **Step 8: Commit**

```bash
git add src/information/core src/information/api/deps.py alembic_information.ini alembic_information
git commit -m "#138 feat: Information service db engine + alembic root + deps stub"
```

---

## Task 3: InfoItem model + migration + tests

**Files:**
- Create: `src/information/core/models/info_item.py`
- Create: `alembic_information/versions/<hash>_create_info_items.py` (autogenerated)
- Modify: `src/information/core/models/__init__.py` — re-export InfoItem
- Create: `tests/information/conftest.py`
- Create: `tests/information/core/__init__.py`
- Create: `tests/information/core/test_models.py`

- [ ] **Step 1: Define the InfoItem model**

`src/information/core/models/info_item.py`:
```python
"""Information Item — the stable, externally-named target being tracked."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.information.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class InfoItem(Base, TimestampMixin):
    """An Information Item — one specific thing being tracked."""

    __tablename__ = "info_items"
    __table_args__ = {"schema": "information"}

    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType(), primary_key=True, default=generate_ulid
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
```

- [ ] **Step 2: Re-export from package init**

Edit `src/information/core/models/__init__.py`:
```python
"""Information service ORM models."""

from src.information.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.information.core.models.info_item import InfoItem

__all__ = ["Base", "InfoItem", "TimestampMixin", "ULIDType", "generate_ulid"]
```

- [ ] **Step 3: Generate the migration**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic -c alembic_information.ini revision --autogenerate -m "create info_items"
```

Verify the generated file under `alembic_information/versions/` creates the `information.info_items` table with the right columns (info_item_id String(26) PK, name, description, owner, created_at, updated_at) and **does not** include any `op.drop_table(...)` operations targeting Watcher's tables — if it does, the `include_object` filter from Task 2 isn't matching correctly.

The migration must also create the `information` schema before the table. Add this as the first operation of `upgrade()` (manually edit if autogenerate doesn't include it):

```python
def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS information")
    op.create_table(
        "info_items",
        # ... columns ...
        schema="information",
    )
```

And the inverse for `downgrade()`:

```python
def downgrade() -> None:
    op.drop_table("info_items", schema="information")
    op.execute("DROP SCHEMA IF EXISTS information CASCADE")
```

- [ ] **Step 4: Apply the migration**

```bash
uv run alembic -c alembic_information.ini upgrade head
```

Expected: `Running upgrade  -> <hash>, create info_items`.

- [ ] **Step 5: Write the conftest fixtures**

`tests/information/conftest.py`:
```python
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
```

- [ ] **Step 6: Write the failing model test**

`tests/information/core/test_models.py`:
```python
"""InfoItem ORM round-trip tests."""

import pytest
from sqlalchemy import select

from src.information.core.models import InfoItem


@pytest.mark.asyncio
async def test_info_item_round_trip(session):
    item = InfoItem(name="Colorado active licenses", description="Roster page", owner="greg")
    session.add(item)
    await session.commit()

    result = await session.execute(select(InfoItem).where(InfoItem.info_item_id == item.info_item_id))
    fetched = result.scalar_one()
    assert fetched.name == "Colorado active licenses"
    assert fetched.description == "Roster page"
    assert fetched.owner == "greg"
    assert str(fetched.info_item_id)  # ULID generated
```

- [ ] **Step 7: Run test to verify it passes**

`get_db_session` already exists from Task 2. Run:
```bash
uv run pytest tests/information/core/test_models.py -v --no-cov
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/information tests/information alembic_information/versions
git commit -m "#138 feat: InfoItem model + migration + db fixtures"
```

---

## Task 4: API-key auth dependency (single shared secret) + tests

**Files:**
- Modify: `src/information/api/deps.py` — add `require_api_key`
- Create: `tests/information/api/test_auth.py`

**Why a shared secret instead of an `api_keys` table?** Phase 1 prototype scope. Real multi-tenant API key management is deferred to a later phase; the `X-API-Key` header shape is preserved so the migration is non-breaking.

- [ ] **Step 1: Write the failing auth tests**

`tests/information/api/test_auth.py`:
```python
"""X-API-Key bearer auth tests for the Information service."""

import os

import pytest


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_missing_key_returns_403(client):
    response = await client.get("/api/v1/info-items")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invalid_key_returns_401(client):
    response = await client.get(
        "/api/v1/info-items", headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_passes(client):
    response = await client.get(
        "/api/v1/info-items", headers={"X-API-Key": "test-secret-key"}
    )
    assert response.status_code == 200  # empty list
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: 404 (no `/api/v1/info-items` route yet). That's fine — they'll start passing once Task 5 ships the route under the auth-protected v1 router.

- [ ] **Step 3: Add `require_api_key` to deps**

Append to `src/information/api/deps.py`:
```python
import os

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(raw_key: str | None = Depends(api_key_header)) -> None:
    """Validate X-API-Key against INFORMATION_API_KEY env var.

    Raises 403 when the header is absent and 401 when it is present but invalid.
    """
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    expected = os.environ.get("INFORMATION_API_KEY")
    if not expected or raw_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
```

- [ ] **Step 4: Wire the v1 router into main.py**

Edit `src/information/api/main.py`:
```python
"""Information service — FastAPI application entry point."""

from fastapi import APIRouter, Depends, FastAPI

from src.information.api.deps import require_api_key
from src.information.api.routes.health import router as health_router
from src.information.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="information", version="0.1.0")

v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
# Routers attach in later tasks (info_items, info_specs).

app.include_router(v1_router)
app.include_router(health_router)
```

- [ ] **Step 5: Commit (auth tests will continue to fail until Task 5 — that's expected; document in the commit)**

```bash
git add src/information/api/deps.py src/information/api/main.py tests/information/api/test_auth.py
git commit -m "#138 feat: X-API-Key bearer auth + v1 router scaffolding

Auth tests intentionally still fail (no info-items route yet); they
flip to passing in Task 5."
```

---

## Task 5: InfoItem routes (POST/GET) + Pydantic schemas

**Files:**
- Create: `src/information/api/schemas/__init__.py`
- Create: `src/information/api/schemas/info_item.py`
- Create: `src/information/api/routes/info_items.py`
- Modify: `src/information/api/main.py` — include info_items router
- Create: `tests/information/api/test_info_items.py`

- [ ] **Step 1: Write Pydantic schemas**

`src/information/api/schemas/info_item.py`:
```python
"""Pydantic IO schemas for InfoItem endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class InfoItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=200)


class InfoItemOut(BaseModel):
    info_item_id: str
    name: str
    description: str | None
    owner: str | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: Write the failing route tests**

`tests/information/api/test_info_items.py`:
```python
"""InfoItem CRUD route tests."""

import pytest


HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


@pytest.mark.asyncio
async def test_create_info_item(client):
    response = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "Colorado active licenses", "owner": "greg"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Colorado active licenses"
    assert body["owner"] == "greg"
    assert body["description"] is None
    assert len(body["info_item_id"]) == 26  # ULID length


@pytest.mark.asyncio
async def test_get_info_item(client):
    create = await client.post(
        "/api/v1/info-items", headers=HEADERS, json={"name": "X"}
    )
    item_id = create.json()["info_item_id"]
    get = await client.get(f"/api/v1/info-items/{item_id}", headers=HEADERS)
    assert get.status_code == 200
    assert get.json()["info_item_id"] == item_id


@pytest.mark.asyncio
async def test_get_info_item_404(client):
    response = await client.get(
        "/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ", headers=HEADERS
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_info_items_empty(client):
    response = await client.get("/api/v1/info-items", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == []
```

- [ ] **Step 3: Run tests to verify they fail**

Expected: 404 on all (route doesn't exist yet).

- [ ] **Step 4: Implement the routes**

`src/information/api/routes/info_items.py`:
```python
"""InfoItem CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_item import InfoItemCreate, InfoItemOut
from src.information.core.models import InfoItem

router = APIRouter(prefix="/info-items", tags=["info-items"])


def _to_out(item: InfoItem) -> InfoItemOut:
    return InfoItemOut(
        info_item_id=str(item.info_item_id),
        name=item.name,
        description=item.description,
        owner=item.owner,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post("", response_model=InfoItemOut, status_code=201)
async def create_info_item(
    body: InfoItemCreate, session: AsyncSession = Depends(get_db_session)
) -> InfoItemOut:
    item = InfoItem(name=body.name, description=body.description, owner=body.owner)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return _to_out(item)


@router.get("", response_model=list[InfoItemOut])
async def list_info_items(
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoItemOut]:
    result = await session.execute(select(InfoItem).order_by(InfoItem.created_at))
    return [_to_out(item) for item in result.scalars().all()]


@router.get("/{info_item_id}", response_model=InfoItemOut)
async def get_info_item(
    info_item_id: str, session: AsyncSession = Depends(get_db_session)
) -> InfoItemOut:
    result = await session.execute(
        select(InfoItem).where(InfoItem.info_item_id == info_item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")
    return _to_out(item)
```

- [ ] **Step 5: Wire the router into main.py**

Edit `src/information/api/main.py`:
```python
from src.information.api.routes.info_items import router as info_items_router
# ...
v1_router.include_router(info_items_router)
```

- [ ] **Step 6: Create `src/information/api/schemas/__init__.py`** (empty file)

- [ ] **Step 7: Run all tests to verify they pass**

```bash
uv run pytest tests/information/ -v --no-cov
```

Expected: all tests in test_health, test_auth, test_models, and test_info_items pass.

- [ ] **Step 8: Commit**

```bash
git add src/information/api tests/information/api/test_info_items.py
git commit -m "#138 feat: InfoItem POST/GET/list routes"
```

---

## Task 6: InfoSpec JSON Schema v1 + validator

**Files:**
- Create: `src/information/core/info_spec_schema/__init__.py`
- Create: `src/information/core/info_spec_schema/v1.json`
- Create: `src/information/core/info_spec_schema/validator.py`
- Modify: `pyproject.toml` — add `jsonschema` dependency
- Create: `tests/information/core/test_validator.py`

- [ ] **Step 1: Add `jsonschema` to pyproject.toml**

Find the `dependencies = [` block in `pyproject.toml` and add (alphabetically near `httpx` or `procrastinate`):
```toml
    "jsonschema>=4.21.0,<5",
```

Then run:
```bash
uv sync
```

- [ ] **Step 2: Create the v1 JSON Schema**

`src/information/core/info_spec_schema/v1.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://watcher.exe.xyz/schemas/info-spec/v1.json",
  "title": "Information Source Specification v1",
  "type": "object",
  "required": ["schema_version", "target", "extraction", "fingerprint"],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "target": {
      "type": "object",
      "required": ["url"],
      "additionalProperties": false,
      "properties": {
        "url": {"type": "string", "format": "uri"},
        "fetch": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "render": {"type": "boolean", "default": false},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30}
          }
        }
      }
    },
    "extraction": {
      "type": "object",
      "required": ["algorithm"],
      "additionalProperties": false,
      "properties": {
        "algorithm": {"enum": ["css", "xpath", "jsonpath", "regex", "full_page"]},
        "selector": {"type": "string"}
      },
      "allOf": [
        {
          "if": {"properties": {"algorithm": {"const": "full_page"}}},
          "then": {"not": {"required": ["selector"]}},
          "else": {"required": ["selector"]}
        }
      ]
    },
    "fingerprint": {
      "type": "object",
      "required": ["algorithm"],
      "additionalProperties": false,
      "properties": {
        "algorithm": {"enum": ["sha256", "simhash"]}
      }
    }
  }
}
```

- [ ] **Step 3: Create the validator**

`src/information/core/info_spec_schema/validator.py`:
```python
"""Validate InfoSpec document bodies against the v1 JSON Schema."""

import json
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_v1_schema: dict[str, Any] | None = None


def _load_v1_schema() -> dict[str, Any]:
    global _v1_schema
    if _v1_schema is None:
        text = resources.files("src.information.core.info_spec_schema").joinpath("v1.json").read_text()
        _v1_schema = json.loads(text)
    return _v1_schema


class InfoSpecValidationError(ValueError):
    """Raised when a document fails InfoSpec schema validation."""


def validate_info_spec(document: dict[str, Any]) -> None:
    """Raise InfoSpecValidationError if document is invalid against the declared schema_version.

    Currently supports schema_version=1 only.
    """
    schema_version = document.get("schema_version")
    if schema_version != 1:
        raise InfoSpecValidationError(
            f"Unsupported schema_version: {schema_version!r} (expected 1)"
        )
    schema = _load_v1_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: e.path)
    if errors:
        details = "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
        raise InfoSpecValidationError(f"InfoSpec invalid: {details}")
```

- [ ] **Step 4: Create `src/information/core/info_spec_schema/__init__.py`**

```python
"""InfoSpec JSON Schema definitions and validator."""

from src.information.core.info_spec_schema.validator import (
    InfoSpecValidationError,
    validate_info_spec,
)

__all__ = ["InfoSpecValidationError", "validate_info_spec"]
```

- [ ] **Step 5: Write validator tests**

`tests/information/core/test_validator.py`:
```python
"""Validator tests for InfoSpec v1 documents."""

import pytest

from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)


def _minimal_valid() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com/page"},
        "extraction": {"algorithm": "css", "selector": ".content"},
        "fingerprint": {"algorithm": "sha256"},
    }


def test_minimal_valid_doc_passes():
    validate_info_spec(_minimal_valid())


def test_full_page_does_not_require_selector():
    doc = _minimal_valid()
    doc["extraction"] = {"algorithm": "full_page"}
    validate_info_spec(doc)


def test_css_requires_selector():
    doc = _minimal_valid()
    doc["extraction"] = {"algorithm": "css"}
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_unknown_algorithm_rejected():
    doc = _minimal_valid()
    doc["extraction"]["algorithm"] = "magic"
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_unknown_schema_version_rejected():
    doc = _minimal_valid()
    doc["schema_version"] = 2
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_missing_url_rejected():
    doc = _minimal_valid()
    doc["target"] = {}
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)


def test_extra_top_level_key_rejected():
    doc = _minimal_valid()
    doc["unexpected"] = "field"
    with pytest.raises(InfoSpecValidationError):
        validate_info_spec(doc)
```

- [ ] **Step 6: Run validator tests to verify all pass**

```bash
uv run pytest tests/information/core/test_validator.py -v --no-cov
```

Expected: 7 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/information/core/info_spec_schema tests/information/core/test_validator.py pyproject.toml uv.lock
git commit -m "#138 feat: InfoSpec v1 JSON Schema + validator"
```

---

## Task 7: InfoSpec model + migration

**Files:**
- Create: `src/information/core/models/info_spec.py`
- Modify: `src/information/core/models/__init__.py` — re-export InfoSpec
- Create: `alembic_information/versions/<hash>_create_info_specs.py` (autogenerated)
- Create: `tests/information/core/test_info_spec_model.py`

- [ ] **Step 1: Define the InfoSpec model**

`src/information/core/models/info_spec.py`:
```python
"""Information Source Specification — one way to source an InfoItem."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.information.core.models.base import Base, ULIDType, generate_ulid


class InfoSpec(Base):
    """An InfoSpec — describes one way to source a parent InfoItem.

    Document body (JSONB column) is immutable. Placement metadata
    (priority, active) is mutable.
    """

    __tablename__ = "info_specs"

    info_spec_id: Mapped[ULID] = mapped_column(
        ULIDType(), primary_key=True, default=generate_ulid
    )
    info_item_id: Mapped[ULID] = mapped_column(
        ULIDType(),
        ForeignKey("information.info_items.info_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "uq_info_specs_active_priority_per_item",
            "info_item_id",
            "priority",
            unique=True,
            postgresql_where=text("active"),
        ),
        {"schema": "information"},
    )
```

- [ ] **Step 2: Re-export from package init**

Edit `src/information/core/models/__init__.py`:
```python
"""Information service ORM models."""

from src.information.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid
from src.information.core.models.info_item import InfoItem
from src.information.core.models.info_spec import InfoSpec

__all__ = ["Base", "InfoItem", "InfoSpec", "TimestampMixin", "ULIDType", "generate_ulid"]
```

- [ ] **Step 3: Generate the migration**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic -c alembic_information.ini revision --autogenerate -m "create info_specs"
```

Inspect the generated file. Verify:
- `info_specs` table created with `schema="information"` and all columns
- Foreign key to `information.info_items.info_item_id`
- Partial unique index `uq_info_specs_active_priority_per_item` with `WHERE active`
- No spurious drops targeting Watcher tables

If the partial index isn't autogenerated correctly, edit the migration to add it explicitly:
```python
op.create_index(
    "uq_info_specs_active_priority_per_item",
    "info_specs",
    ["info_item_id", "priority"],
    unique=True,
    postgresql_where=sa.text("active"),
    schema="information",
)
```

- [ ] **Step 4: Apply the migration**

```bash
uv run alembic -c alembic_information.ini upgrade head
```

- [ ] **Step 5: Write the model test**

`tests/information/core/test_info_spec_model.py`:
```python
"""InfoSpec ORM tests — round-trip + partial unique constraint."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.information.core.models import InfoItem, InfoSpec


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


@pytest.mark.asyncio
async def test_info_spec_round_trip(session):
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1,
        document=_doc(),
        priority=1,
        active=True,
    )
    session.add(spec)
    await session.commit()

    result = await session.execute(select(InfoSpec).where(InfoSpec.info_spec_id == spec.info_spec_id))
    fetched = result.scalar_one()
    assert fetched.priority == 1
    assert fetched.active is True
    assert fetched.document["target"]["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_partial_unique_active_priority_blocks_duplicate(session):
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec1 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1, document=_doc(), priority=1, active=True,
    )
    session.add(spec1)
    await session.commit()

    spec2 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1, document=_doc(), priority=1, active=True,
    )
    session.add(spec2)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_inactive_specs_can_share_priority(session):
    """Two inactive specs at priority=1 should NOT violate the partial unique."""
    item = InfoItem(name="A")
    session.add(item)
    await session.flush()

    spec1 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1, document=_doc(), priority=1, active=False,
    )
    spec2 = InfoSpec(
        info_item_id=item.info_item_id,
        schema_version=1, document=_doc(), priority=1, active=False,
    )
    session.add_all([spec1, spec2])
    await session.commit()  # should succeed
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/information/core/test_info_spec_model.py -v --no-cov
```

- [ ] **Step 7: Commit**

```bash
git add src/information tests/information alembic_information/versions
git commit -m "#138 feat: InfoSpec model with priority/active + partial unique index"
```

---

## Task 8: POST /info-items/{id}/info-specs (create InfoSpec) with validation + auto-priority

**Files:**
- Create: `src/information/api/schemas/info_spec.py`
- Create: `src/information/api/routes/info_specs.py`
- Modify: `src/information/api/main.py` — include router
- Create: `tests/information/api/test_info_specs_create.py`

**Server-side priority semantics for create:**
- If the request body omits `priority`, the server assigns `max(active priorities for this item) + 1`, or 1 if none.
- If the request body specifies `priority`, the server inserts at that priority. If a conflicting active row exists at that priority, the conflict resolution is to **demote the conflicting row +1** (and cascade the shift). Document the alternative ("deactivate the displaced row") as a follow-up; default Phase 1 behavior is shift-down.

- [ ] **Step 1: Pydantic schemas for InfoSpec IO**

`src/information/api/schemas/info_spec.py`:
```python
"""Pydantic IO schemas for InfoSpec endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InfoSpecCreate(BaseModel):
    document: dict[str, Any]
    priority: int | None = Field(default=None, ge=1)


class InfoSpecOut(BaseModel):
    info_spec_id: str
    info_item_id: str
    schema_version: int
    document: dict[str, Any]
    priority: int
    active: bool
    created_at: datetime


class InfoSpecPatch(BaseModel):
    priority: int | None = Field(default=None, ge=1)
    active: bool | None = None
```

- [ ] **Step 2: Write the failing route tests**

`tests/information/api/test_info_specs_create.py`:
```python
"""POST /info-items/{id}/info-specs tests."""

import pytest


HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


async def _create_item(client) -> str:
    r = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    return r.json()["info_item_id"]


@pytest.mark.asyncio
async def test_create_first_info_spec_default_priority_1(client):
    item_id = await _create_item(client)
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc()},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["priority"] == 1
    assert body["active"] is True
    assert body["info_item_id"] == item_id
    assert body["schema_version"] == 1


@pytest.mark.asyncio
async def test_create_second_default_priority_2(client):
    item_id = await _create_item(client)
    await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS, json={"document": _doc()},
    )
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS, json={"document": _doc()},
    )
    assert r.json()["priority"] == 2


@pytest.mark.asyncio
async def test_create_explicit_priority_demotes_existing(client):
    item_id = await _create_item(client)
    first = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS, json={"document": _doc()},
    )
    first_id = first.json()["info_spec_id"]

    second = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS, json={"document": _doc(), "priority": 1},
    )
    assert second.status_code == 201
    assert second.json()["priority"] == 1

    # Original first should now be priority 2
    list_r = await client.get(
        f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS
    )
    by_id = {s["info_spec_id"]: s["priority"] for s in list_r.json()}
    assert by_id[first_id] == 2


@pytest.mark.asyncio
async def test_invalid_document_returns_422(client):
    item_id = await _create_item(client)
    bad_doc = {"schema_version": 1}  # missing target, extraction, fingerprint
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS, json={"document": bad_doc},
    )
    assert r.status_code == 422
    assert "InfoSpec invalid" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_for_unknown_info_item_returns_404(client):
    r = await client.post(
        "/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ/info-specs",
        headers=HEADERS, json={"document": _doc()},
    )
    assert r.status_code == 404
```

Note: `test_create_explicit_priority_demotes_existing` uses `GET /info-items/{id}/info-specs` (the list endpoint). The implementation step below ships the list route alongside create — it's trivial and needed by this task's tests.

- [ ] **Step 3: Implement create + list routes**

`src/information/api/routes/info_specs.py`:
```python
"""InfoSpec CRUD endpoints (nested under InfoItem)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.schemas.info_spec import (
    InfoSpecCreate,
    InfoSpecOut,
    InfoSpecPatch,
)
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)
from src.information.core.models import InfoItem, InfoSpec

router = APIRouter(prefix="/info-items/{info_item_id}", tags=["info-specs"])


def _to_out(spec: InfoSpec) -> InfoSpecOut:
    return InfoSpecOut(
        info_spec_id=str(spec.info_spec_id),
        info_item_id=str(spec.info_item_id),
        schema_version=spec.schema_version,
        document=spec.document,
        priority=spec.priority,
        active=spec.active,
        created_at=spec.created_at,
    )


async def _ensure_item_exists(session: AsyncSession, info_item_id: str) -> InfoItem:
    result = await session.execute(
        select(InfoItem).where(InfoItem.info_item_id == info_item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="InfoItem not found")
    return item


@router.post("/info-specs", response_model=InfoSpecOut, status_code=201)
async def create_info_spec(
    info_item_id: str,
    body: InfoSpecCreate,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    await _ensure_item_exists(session, info_item_id)

    try:
        validate_info_spec(body.document)
    except InfoSpecValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    schema_version = body.document["schema_version"]

    # Determine target priority
    if body.priority is None:
        max_p = await session.scalar(
            select(func.coalesce(func.max(InfoSpec.priority), 0)).where(
                InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True)
            )
        )
        target_priority = max_p + 1
    else:
        target_priority = body.priority
        # Shift active rows at >= target_priority by +1 to make room.
        # IMPORTANT: shift in DESCENDING priority order so each UPDATE moves a
        # row into a slot that was just vacated by the previous UPDATE.
        # Ascending order would transiently violate the partial unique index
        # `(info_item_id, priority) WHERE active`.
        rows = (await session.execute(
            select(InfoSpec).where(
                InfoSpec.info_item_id == info_item_id,
                InfoSpec.active.is_(True),
                InfoSpec.priority >= target_priority,
            ).order_by(InfoSpec.priority.desc())
        )).scalars().all()
        for row in rows:
            row.priority = row.priority + 1
        await session.flush()

    spec = InfoSpec(
        info_item_id=info_item_id,
        schema_version=schema_version,
        document=body.document,
        priority=target_priority,
        active=True,
    )
    session.add(spec)
    await session.commit()
    await session.refresh(spec)
    return _to_out(spec)


@router.get("/info-specs", response_model=list[InfoSpecOut])
async def list_info_specs(
    info_item_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoSpecOut]:
    await _ensure_item_exists(session, info_item_id)
    result = await session.execute(
        select(InfoSpec)
        .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
        .order_by(InfoSpec.priority.asc())
    )
    return [_to_out(s) for s in result.scalars().all()]
```

- [ ] **Step 4: Wire the router into main.py**

```python
from src.information.api.routes.info_specs import router as info_specs_router
# ...
v1_router.include_router(info_specs_router)
```

- [ ] **Step 5: Run tests to verify all pass**

```bash
uv run pytest tests/information/api/test_info_specs_create.py -v --no-cov
```

Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/information/api tests/information/api/test_info_specs_create.py
git commit -m "#138 feat: POST /info-items/{id}/info-specs with validation + priority shift"
```

---

## Task 9: GET /info-items/{id}/primary-info-spec (hot path)

**Files:**
- Modify: `src/information/api/routes/info_specs.py` — add `primary_info_spec` route
- Create: `tests/information/api/test_primary_info_spec.py`

- [ ] **Step 1: Write the failing tests**

`tests/information/api/test_primary_info_spec.py`:
```python
"""GET /info-items/{id}/primary-info-spec tests."""

import pytest


HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


async def _create_item(client) -> str:
    r = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    return r.json()["info_item_id"]


async def _create_spec(client, item_id: str, priority: int | None = None) -> dict:
    payload = {"document": _doc()}
    if priority is not None:
        payload["priority"] = priority
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS, json=payload
    )
    return r.json()


@pytest.mark.asyncio
async def test_primary_returns_priority_1(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)
    await _create_spec(client, item_id)  # second at priority 2
    r = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert r.status_code == 200
    assert r.json()["info_spec_id"] == first["info_spec_id"]
    assert r.json()["priority"] == 1


@pytest.mark.asyncio
async def test_primary_404_when_none(client):
    item_id = await _create_item(client)
    r = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_primary_404_when_unknown_info_item(client):
    r = await client.get(
        "/api/v1/info-items/01HZZZZZZZZZZZZZZZZZZZZZZZ/primary-info-spec",
        headers=HEADERS,
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: 404 on the primary endpoint (route doesn't exist).

- [ ] **Step 3: Add the primary route**

Append to `src/information/api/routes/info_specs.py`:
```python
@router.get("/primary-info-spec", response_model=InfoSpecOut)
async def get_primary_info_spec(
    info_item_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    """Return the lowest-priority active InfoSpec for the InfoItem.

    Hot path for consumer services (Watcher, Archive).
    """
    await _ensure_item_exists(session, info_item_id)
    result = await session.execute(
        select(InfoSpec)
        .where(InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True))
        .order_by(InfoSpec.priority.asc())
        .limit(1)
    )
    spec = result.scalar_one_or_none()
    if spec is None:
        raise HTTPException(status_code=404, detail="No active InfoSpec for InfoItem")
    return _to_out(spec)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/information/api/test_primary_info_spec.py -v --no-cov
```

- [ ] **Step 5: Commit**

```bash
git add src/information/api/routes/info_specs.py tests/information/api/test_primary_info_spec.py
git commit -m "#138 feat: GET /info-items/{id}/primary-info-spec (hot path)"
```

---

## Task 10: PATCH /info-items/{id}/info-specs/{spec_id} — priority/active mutations

**Files:**
- Modify: `src/information/api/routes/info_specs.py` — add PATCH route
- Create: `tests/information/api/test_info_specs_patch.py`

**Semantics:**
- PATCH may include `priority`, `active`, or both. The document body cannot be patched (immutability).
- Setting `priority` while `active=true` shifts conflicting active rows by +1 (same logic as create).
- Setting `active=false` does NOT renumber surviving active rows (priorities can become non-contiguous; that's fine).
- Setting `active=true` on a previously inactive spec requires either (a) an explicit `priority` not in use by an active row, or (b) defaults to `max(active priority) + 1` if `priority` is omitted.

- [ ] **Step 1: Write the failing tests**

`tests/information/api/test_info_specs_patch.py`:
```python
"""PATCH /info-items/{id}/info-specs/{spec_id} tests."""

import pytest


HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc() -> dict:
    return {
        "schema_version": 1,
        "target": {"url": "https://example.com"},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


async def _create_item(client) -> str:
    r = await client.post("/api/v1/info-items", headers=HEADERS, json={"name": "X"})
    return r.json()["info_item_id"]


async def _create_spec(client, item_id: str, priority: int | None = None) -> dict:
    payload = {"document": _doc()}
    if priority is not None:
        payload["priority"] = priority
    r = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS, json=payload
    )
    return r.json()


@pytest.mark.asyncio
async def test_patch_active_false_demotes_primary(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)         # priority=1
    second = await _create_spec(client, item_id)        # priority=2

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS, json={"active": False},
    )
    assert r.status_code == 200
    assert r.json()["active"] is False

    primary = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert primary.json()["info_spec_id"] == second["info_spec_id"]
    assert primary.json()["priority"] == 2  # surviving spec keeps priority 2


@pytest.mark.asyncio
async def test_patch_priority_swaps(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)
    second = await _create_spec(client, item_id)

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{second['info_spec_id']}",
        headers=HEADERS, json={"priority": 1},
    )
    assert r.status_code == 200
    assert r.json()["priority"] == 1

    primary = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert primary.json()["info_spec_id"] == second["info_spec_id"]


@pytest.mark.asyncio
async def test_patch_unknown_returns_404(client):
    item_id = await _create_item(client)
    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/01HZZZZZZZZZZZZZZZZZZZZZZZ",
        headers=HEADERS, json={"active": False},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reactivate_with_default_priority_appends(client):
    item_id = await _create_item(client)
    first = await _create_spec(client, item_id)
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS, json={"active": False},
    )
    second = await _create_spec(client, item_id)  # becomes priority 1 (active)

    r = await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{first['info_spec_id']}",
        headers=HEADERS, json={"active": True},
    )
    assert r.status_code == 200
    assert r.json()["priority"] == 2
    assert r.json()["active"] is True
```

- [ ] **Step 2: Implement the PATCH route**

Append to `src/information/api/routes/info_specs.py`:
```python
@router.patch("/info-specs/{info_spec_id}", response_model=InfoSpecOut)
async def patch_info_spec(
    info_item_id: str,
    info_spec_id: str,
    body: InfoSpecPatch,
    session: AsyncSession = Depends(get_db_session),
) -> InfoSpecOut:
    """Mutate placement metadata (priority, active). Document body is immutable."""
    await _ensure_item_exists(session, info_item_id)

    result = await session.execute(
        select(InfoSpec).where(
            InfoSpec.info_spec_id == info_spec_id,
            InfoSpec.info_item_id == info_item_id,
        )
    )
    spec = result.scalar_one_or_none()
    if spec is None:
        raise HTTPException(status_code=404, detail="InfoSpec not found")

    target_active = body.active if body.active is not None else spec.active
    target_priority = body.priority

    if target_active and target_priority is None and not spec.active:
        # Reactivating without explicit priority: append at end.
        max_p = await session.scalar(
            select(func.coalesce(func.max(InfoSpec.priority), 0)).where(
                InfoSpec.info_item_id == info_item_id, InfoSpec.active.is_(True)
            )
        )
        target_priority = max_p + 1

    if target_active and target_priority is not None and target_priority != spec.priority:
        # Shift active rows at >= target_priority (excluding self) by +1.
        # Descending order — see comment in create_info_spec for why.
        rows = (await session.execute(
            select(InfoSpec).where(
                InfoSpec.info_item_id == info_item_id,
                InfoSpec.active.is_(True),
                InfoSpec.priority >= target_priority,
                InfoSpec.info_spec_id != info_spec_id,
            ).order_by(InfoSpec.priority.desc())
        )).scalars().all()
        for row in rows:
            row.priority = row.priority + 1
        await session.flush()

    spec.active = target_active
    if target_priority is not None:
        spec.priority = target_priority

    await session.commit()
    await session.refresh(spec)
    return _to_out(spec)
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
uv run pytest tests/information/api/test_info_specs_patch.py -v --no-cov
```

- [ ] **Step 4: Commit**

```bash
git add src/information/api/routes/info_specs.py tests/information/api/test_info_specs_patch.py
git commit -m "#138 feat: PATCH /info-items/{id}/info-specs/{spec_id} priority+active"
```

---

## Task 11: End-to-end smoke test

**Files:**
- Create: `tests/information/api/test_smoke_e2e.py`

- [ ] **Step 1: Write the smoke test**

`tests/information/api/test_smoke_e2e.py`:
```python
"""End-to-end smoke test — exercises the full Phase 1 contract."""

import pytest


HEADERS = {"X-API-Key": "test-secret-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("INFORMATION_API_KEY", "test-secret-key")


def _doc(url: str = "https://example.com") -> dict:
    return {
        "schema_version": 1,
        "target": {"url": url},
        "extraction": {"algorithm": "css", "selector": ".x"},
        "fingerprint": {"algorithm": "sha256"},
    }


@pytest.mark.asyncio
async def test_full_phase1_round_trip(client):
    # 1. Create an InfoItem
    item_resp = await client.post(
        "/api/v1/info-items",
        headers=HEADERS,
        json={"name": "Colorado active licenses", "owner": "greg"},
    )
    assert item_resp.status_code == 201
    item_id = item_resp.json()["info_item_id"]

    # 2. Create primary InfoSpec
    primary_resp = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc("https://example.com/primary")},
    )
    assert primary_resp.status_code == 201
    primary_id = primary_resp.json()["info_spec_id"]

    # 3. GET primary returns it
    p = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert p.json()["info_spec_id"] == primary_id

    # 4. Add a fallback at priority 2
    fb_resp = await client.post(
        f"/api/v1/info-items/{item_id}/info-specs",
        headers=HEADERS,
        json={"document": _doc("https://example.com/fallback")},
    )
    fallback_id = fb_resp.json()["info_spec_id"]
    assert fb_resp.json()["priority"] == 2

    # 5. List returns both, ordered
    list_resp = await client.get(
        f"/api/v1/info-items/{item_id}/info-specs", headers=HEADERS
    )
    listed = list_resp.json()
    assert [s["info_spec_id"] for s in listed] == [primary_id, fallback_id]

    # 6. Deactivate the primary → fallback becomes the new primary
    await client.patch(
        f"/api/v1/info-items/{item_id}/info-specs/{primary_id}",
        headers=HEADERS, json={"active": False},
    )
    p2 = await client.get(
        f"/api/v1/info-items/{item_id}/primary-info-spec", headers=HEADERS
    )
    assert p2.json()["info_spec_id"] == fallback_id
```

- [ ] **Step 2: Run the smoke test**

```bash
uv run pytest tests/information/api/test_smoke_e2e.py -v --no-cov
```

Expected: PASS.

- [ ] **Step 3: Run the full Information service test suite**

```bash
uv run pytest tests/information/ -v --no-cov
```

Expected: every test passes (validator + models + auth + info_items + info_specs create/list/primary/patch + smoke).

- [ ] **Step 4: Commit**

```bash
git add tests/information/api/test_smoke_e2e.py
git commit -m "#138 test: Phase 1 end-to-end smoke (create → primary → fallback → demote)"
```

---

## Task 12: Systemd unit + deployment plumbing

**Files:**
- Create: `deploy/information.service`
- Modify: `AGENTS.md` — add Information service to Infrastructure + Server Lifecycle sections

**Note:** This task creates the unit file and documents how to install it. **The plan does not run `sudo systemctl ...` commands** — the implementer should report that the unit is ready and ask the user to install + start it manually (matches Watcher's deployment discipline).

- [ ] **Step 1: Verify the `uv` binary path**

```bash
which uv
```

Confirm it matches `/usr/local/bin/uv` (the path used in `deploy/watcher.service`). If it differs, use the correct path in the unit file below.

- [ ] **Step 2: Create `deploy/information.service`**

```
[Unit]
Description=Information service — Cannabis Observer information registry
After=network.target postgresql.service

[Service]
Type=simple
User=exedev
WorkingDirectory=/home/exedev/watcher

ExecStartPre=+/bin/bash -c 'mkdir -p /run/information && chown exedev:exedev /run/information'
ExecStartPre=/bin/bash -c 'echo BUILD_ID=$(git rev-parse --short HEAD) > /run/information/build-id'

EnvironmentFile=-/run/information/build-id
EnvironmentFile=-/etc/information/.env
EnvironmentFile=/etc/watcher/.env
EnvironmentFile=-/home/exedev/watcher/.env

ExecStart=/usr/local/bin/uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8020
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Update AGENTS.md**

Add a row to the **Infrastructure** table:
```markdown
| Information service | 8020 | `systemctl` (`information.service`) |
```

Add to the **Server Lifecycle** section a paragraph:
```markdown
**Information service.** Owns the canonical Information Item + InfoSpec
registry. Lives at `src/information/`. Runs as `information.service` on
port 8020 once installed. Migrations: `uv run alembic -c alembic_information.ini upgrade head`.
Dev server: `uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8021 --reload`.
```

- [ ] **Step 4: Update docs/COMMANDS.md (if it documents alembic)**

Add the Information service alembic command alongside Watcher's:
```bash
uv run alembic -c alembic_information.ini upgrade head     # Information service migrations
uv run alembic -c alembic_information.ini revision --autogenerate -m "msg"
```

- [ ] **Step 5: Commit**

```bash
git add deploy/information.service AGENTS.md docs/COMMANDS.md
git commit -m "#138 docs: deploy information.service unit + AGENTS update"
```

---

## Task 13: Final verification + dev server smoke

**Files:** none modified.

- [ ] **Step 1: Run the full Watcher + Information test suites together**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest --no-cov -m "not integration"
```

Expected: All Watcher tests still pass (none modified) + all Information tests pass.

- [ ] **Step 2: Lint check**

```bash
uv run ruff check src/information tests/information alembic_information
uv run ruff format --check src/information tests/information alembic_information
```

Fix any issues, commit if changes were needed.

- [ ] **Step 3: Boot the Information service against a dev port**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
export INFORMATION_API_KEY=dev-secret-please-change
uv run alembic -c alembic_information.ini upgrade head
uv run uvicorn src.information.api.main:app --host 0.0.0.0 --port 8021 &
sleep 2

# Verify it's up
curl -s http://localhost:8021/health
curl -s -H "X-API-Key: dev-secret-please-change" http://localhost:8021/api/v1/info-items
```

Expected: `{"status":"ok"}` and `[]`.

- [ ] **Step 4: Stop the dev server**

```bash
lsof -ti :8021 | xargs -r kill -9 2>/dev/null
```

- [ ] **Step 5: Final commit (if any docs/lint adjustments were made)**

```bash
git status
# Commit any remaining housekeeping changes.
```

- [ ] **Step 6: Push the branch**

```bash
git push -u origin feat/138-information-service-phase1
```

---

## Wrap-up

After Task 13 completes:
- The Information service prototype is fully runnable on port 8020 (production via systemd) or 8021 (dev).
- All Phase 1 endpoints are tested: health, info-items CRUD, info-specs create + list + primary + patch.
- The InfoSpec v1 JSON Schema is enforced on every create.
- The partial unique index on `(info_item_id, priority) WHERE active` enforces single-primary-per-item at the database level.
- AGENTS.md documents how to operate the service.
- The systemd unit is in place but not yet installed; report this status when the branch lands so the user can `sudo cp` and `sudo systemctl enable --now` it.

**Ready for Phase 2** (Information SDK + Watcher consumer model + Change bus) once this lands on `main`.
