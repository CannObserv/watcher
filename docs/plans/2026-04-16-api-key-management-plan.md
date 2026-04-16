# API Key Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add API key management and public API access to watcher — `app_users` + `api_keys` tables, `X-API-Key` auth on the existing `/api/v1/` router, exe.dev auth guard on the dashboard, and an HTMX Settings page for key CRUD.

**Architecture:** Two new SQLAlchemy models (`AppUser`, `ApiKey`) with a single Alembic migration. A new `src/dashboard/deps.py` provides the `get_dashboard_user` dependency (exe.dev header guard + lazy upsert) added to all dashboard routes. A new `src/api/deps.py` provides `require_api_key` added to the existing `v1_router`. Settings routes and templates live in a new `src/dashboard/settings.py`.

**Tech Stack:** SQLAlchemy 2 async, FastAPI Depends, Alembic, HTMX, Jinja2, Tailwind v4, `hashlib.sha256`, `os.urandom`, `fastapi.security.APIKeyHeader`

---

## File Map

**Create:**
- `src/core/models/app_user.py` — `AppUser` SQLAlchemy model
- `src/core/models/api_key.py` — `ApiKey` SQLAlchemy model
- `alembic/versions/<rev>_add_app_users_and_api_keys.py` — migration (generated)
- `src/dashboard/deps.py` — `get_dashboard_user` dep + `generate_api_key` helper
- `src/api/deps.py` — `require_api_key` dep
- `src/dashboard/settings.py` — settings page + API key CRUD routes
- `src/dashboard/templates/pages/settings.html` — settings landing page
- `src/dashboard/templates/pages/settings_api_keys.html` — API keys list page
- `src/dashboard/templates/partials/api_key_row.html` — read-only key row
- `src/dashboard/templates/partials/api_key_edit_row.html` — edit/new form row
- `src/dashboard/templates/partials/api_key_new_key_modal.html` — one-time key modal
- `tests/dashboard/test_settings_api_keys.py` — integration tests for settings routes
- `tests/api/test_api_key_auth.py` — integration tests for `require_api_key`

**Modify:**
- `src/core/models/__init__.py` — add `AppUser`, `ApiKey` imports and `__all__` entries
- `src/api/main.py` — add `require_api_key` to `v1_router`; include settings router
- `src/dashboard/__init__.py` — include `settings_router` from `src.dashboard.settings`
- `src/dashboard/routes.py` — add `dependencies=[Depends(get_dashboard_user)]` to `router`
- `src/dashboard/templates/base.html` — add Settings nav link after Notifications (lines 32 and 74 in desktop + mobile nav)
- `tests/conftest.py` — override `get_dashboard_user` and `require_api_key` in `client` fixture

---

## Task 1: AppUser and ApiKey models

**Files:**
- Create: `src/core/models/app_user.py`
- Create: `src/core/models/api_key.py`
- Modify: `src/core/models/__init__.py`

- [ ] **Step 1: Write failing model import test**

```python
# tests/core/models/test_api_key_model.py
def test_app_user_model_importable():
    from src.core.models.app_user import AppUser
    u = AppUser(id="usr_1", email="a@b.com")
    assert u.id == "usr_1"
    assert u.email == "a@b.com"

def test_api_key_model_importable():
    from src.core.models.api_key import ApiKey
    k = ApiKey(id="01ABC", user_id="usr_1", label="test", key_prefix="co_abc12", key_hash="deadbeef")
    assert k.label == "test"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/core/models/test_api_key_model.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create `src/core/models/app_user.py`**

```python
"""AppUser model — identity anchor keyed by exe.dev user ID."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base


class AppUser(Base):
    """One row per exe.dev user; created/updated lazily on each dashboard login."""

    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
```

- [ ] **Step 4: Create `src/core/models/api_key.py`**

```python
"""ApiKey model — hashed API credentials owned by an AppUser."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, ULIDType, generate_ulid
from src.core.models.app_user import AppUser  # noqa: F401 (register relationship)


class ApiKey(Base):
    """Stores a SHA-256 hash of each API key; raw key is never persisted."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("app_users.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
```

- [ ] **Step 5: Register models in `src/core/models/__init__.py`**

Add at the top imports:
```python
from src.core.models.app_user import AppUser
from src.core.models.api_key import ApiKey
```

Add to `__all__`:
```python
"AppUser",
"ApiKey",
```

- [ ] **Step 6: Run test to confirm pass**

```bash
uv run pytest tests/core/models/test_api_key_model.py -v
```
Expected: 2 passed

- [ ] **Step 7: Commit**

```bash
git add src/core/models/app_user.py src/core/models/api_key.py src/core/models/__init__.py tests/core/models/test_api_key_model.py
git commit -m "#100 feat: add AppUser and ApiKey SQLAlchemy models"
```

---

## Task 2: Alembic migration

**Files:**
- Create: `alembic/versions/<rev>_add_app_users_and_api_keys.py`

- [ ] **Step 1: Generate migration from models**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
cd /home/exedev/watcher/.worktrees/api-key-management
uv run alembic revision --autogenerate -m "add app_users and api_keys"
```

Expected: new file created in `alembic/versions/`.

- [ ] **Step 2: Review generated migration**

Open the generated file. Verify it creates:
- `app_users` table: `id TEXT PK`, `email TEXT NOT NULL`, `created_at TIMESTAMPTZ`, `updated_at TIMESTAMPTZ`
- `api_keys` table: `id VARCHAR(26) PK`, `user_id TEXT NOT NULL FK→app_users.id`, `label TEXT NOT NULL`, `key_prefix TEXT NOT NULL`, `key_hash TEXT NOT NULL UNIQUE`, `created_at TIMESTAMPTZ`, `last_used_at TIMESTAMPTZ NULL`

If autogenerate added unintended changes (e.g. existing table modifications), remove them.

- [ ] **Step 3: Apply migration to production DB**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
cd /home/exedev/watcher/.worktrees/api-key-management
uv run alembic upgrade head
```

Expected: `Running upgrade ecf003d279f1 -> <new_rev>, add app_users and api_keys`

- [ ] **Step 4: Commit migration**

```bash
git add alembic/versions/
git commit -m "#100 feat: migration — add app_users and api_keys tables"
```

---

## Task 3: Dashboard auth dep (`src/dashboard/deps.py`)

**Files:**
- Create: `src/dashboard/deps.py`
- Create: `tests/dashboard/test_dashboard_deps.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/dashboard/test_dashboard_deps.py
"""Unit tests for dashboard auth dependency."""

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock


class TestGetDashboardUser:
    async def test_missing_user_id_raises_307(self):
        from src.dashboard.deps import get_dashboard_user
        from starlette.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/protected")
        async def protected(user=pytest.importorskip("fastapi").Depends(get_dashboard_user)):
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/protected", follow_redirects=False)
        assert r.status_code == 307
        assert "/__exe.dev/login" in r.headers["location"]

    async def test_missing_email_raises_307(self):
        from src.dashboard.deps import get_dashboard_user
        from starlette.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/protected")
        async def protected(user=pytest.importorskip("fastapi").Depends(get_dashboard_user)):
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/protected", headers={"X-ExeDev-UserID": "usr_1"}, follow_redirects=False)
        assert r.status_code == 307
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/dashboard/test_dashboard_deps.py -v
```
Expected: `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Create `src/dashboard/deps.py`**

```python
"""Dashboard authentication dependencies."""

import hashlib
import os
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.core.models.app_user import AppUser


async def get_dashboard_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> AppUser:
    """Validate exe.dev auth headers; upsert AppUser row; return user.

    Raises 307 → /__exe.dev/login when headers are absent.
    The exe.dev proxy injects X-ExeDev-UserID and X-ExeDev-Email for all
    authenticated visitors; absence means the user is not logged in.
    """
    user_id = request.headers.get("X-ExeDev-UserID")
    email = request.headers.get("X-ExeDev-Email")
    if not user_id or not email:
        path = request.url.path
        query = request.url.query
        next_url = f"{path}?{query}" if query else path
        raise HTTPException(
            status_code=307,
            headers={"Location": f"/__exe.dev/login?redirect={quote(next_url)}"},
        )
    stmt = (
        insert(AppUser)
        .values(id=user_id, email=email)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"email": email, "updated_at": func.now()},
        )
        .returning(AppUser)
    )
    result = await session.execute(stmt)
    user = result.scalar_one()
    await session.commit()
    return user


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix).

    raw_key:    "co_" + 32 hex chars (128-bit random)
    key_hash:   SHA-256 hex of raw_key — stored in DB, never returned again
    key_prefix: first 8 chars of raw_key — stored for display identification
    """
    raw_key = "co_" + os.urandom(16).hex()
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]
    return raw_key, key_hash, key_prefix
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/dashboard/test_dashboard_deps.py -v
```
Expected: 2 passed

- [ ] **Step 5: Add unit tests for `generate_api_key`**

Append to `tests/dashboard/test_dashboard_deps.py`:

```python
import hashlib


class TestGenerateApiKey:
    def test_format(self):
        from src.dashboard.deps import generate_api_key
        raw_key, key_hash, key_prefix = generate_api_key()
        assert raw_key.startswith("co_")
        assert len(raw_key) == 35        # "co_" (3) + 32 hex chars
        assert len(key_hash) == 64       # SHA-256 hex
        assert key_prefix == raw_key[:8]

    def test_is_random(self):
        from src.dashboard.deps import generate_api_key
        r1, _, _ = generate_api_key()
        r2, _, _ = generate_api_key()
        assert r1 != r2

    def test_hash_matches(self):
        from src.dashboard.deps import generate_api_key
        raw_key, key_hash, _ = generate_api_key()
        assert key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
```

- [ ] **Step 6: Run all dep tests**

```bash
uv run pytest tests/dashboard/test_dashboard_deps.py -v
```
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/deps.py tests/dashboard/test_dashboard_deps.py
git commit -m "#100 feat: add dashboard auth dep and generate_api_key helper"
```

---

## Task 4: Apply auth guard to dashboard router + update conftest

**Files:**
- Modify: `src/dashboard/routes.py` (router declaration)
- Modify: `tests/conftest.py` (client fixture)

The existing `router = APIRouter(tags=["dashboard"])` at the top of `routes.py` needs a `dependencies` argument. After adding it, all dashboard tests break because no exe.dev headers are provided; the conftest override silences this.

- [ ] **Step 1: Add guard to `routes.py` router declaration**

In `src/dashboard/routes.py`, update the imports at the top to add:
```python
from src.dashboard.deps import get_dashboard_user
```

Change:
```python
router = APIRouter(tags=["dashboard"])
```
To:
```python
router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_dashboard_user)])
```

- [ ] **Step 2: Run existing dashboard tests — expect failures**

```bash
uv run pytest tests/dashboard/ -v -m integration -x 2>&1 | head -30
```
Expected: failures with `307` or redirect errors.

- [ ] **Step 3: Add `get_dashboard_user` override to `tests/conftest.py`**

In `tests/conftest.py`, add at the top of imports:
```python
from src.dashboard.deps import get_dashboard_user
from src.core.models.app_user import AppUser
```

In the `client` fixture, add the override alongside the existing ones.
The override must NOT use `Depends()` — FastAPI override functions are called directly
without dependency injection. Return a bare `AppUser` instance (no session needed for a
data-only object). Settings tests that need the row in the DB use the `make_app_user`
fixture defined in `test_settings_api_keys.py`.

```python
async def override_dashboard_user():
    return AppUser(id="test-user-id", email="test@example.com")

app.dependency_overrides[get_dashboard_user] = override_dashboard_user
```

- [ ] **Step 4: Run dashboard tests — expect pass**

```bash
uv run pytest tests/dashboard/ -v -m integration 2>&1 | tail -10
```
Expected: all previously-passing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/conftest.py
git commit -m "#100 feat: add exe.dev auth guard to dashboard router"
```

---

## Task 5: `require_api_key` dep + wire into `v1_router`

**Files:**
- Create: `src/api/deps.py`
- Modify: `src/api/main.py`
- Modify: `tests/conftest.py`
- Create: `tests/api/test_api_key_auth.py`

- [ ] **Step 1: Write failing auth tests**

```python
# tests/api/test_api_key_auth.py
"""Integration tests for X-API-Key authentication."""

import hashlib

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.models.api_key import ApiKey
from src.core.models.app_user import AppUser
from src.dashboard.deps import generate_api_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def raw_api_key(db_session):
    """Insert test AppUser + ApiKey; yield raw_key."""
    raw_key, key_hash, key_prefix = generate_api_key()
    # Upsert user
    stmt = (
        pg_insert(AppUser)
        .values(id="test-user-id", email="test@example.com")
        .on_conflict_do_update(index_elements=["id"], set_={"email": "test@example.com"})
    )
    await db_session.execute(stmt)
    db_session.add(ApiKey(
        user_id="test-user-id",
        label="test key",
        key_prefix=key_prefix,
        key_hash=key_hash,
    ))
    await db_session.flush()
    return raw_key


class TestRequireApiKey:
    async def test_valid_key_returns_200(self, client, raw_api_key):
        # Override must be cleared so require_api_key runs for real
        from src.api.deps import require_api_key
        from src.api.main import app
        app.dependency_overrides.pop(require_api_key, None)
        r = await client.get("/api/v1/watches", headers={"X-API-Key": raw_api_key})
        assert r.status_code == 200
        app.dependency_overrides[require_api_key] = lambda: "test-user-id"

    async def test_missing_key_returns_403(self, client):
        from src.api.deps import require_api_key
        from src.api.main import app
        app.dependency_overrides.pop(require_api_key, None)
        r = await client.get("/api/v1/watches")
        assert r.status_code == 403
        app.dependency_overrides[require_api_key] = lambda: "test-user-id"

    async def test_invalid_key_returns_401(self, client):
        from src.api.deps import require_api_key
        from src.api.main import app
        app.dependency_overrides.pop(require_api_key, None)
        r = await client.get("/api/v1/watches", headers={"X-API-Key": "co_notvalid"})
        assert r.status_code == 401
        app.dependency_overrides[require_api_key] = lambda: "test-user-id"

    async def test_valid_key_updates_last_used_at(self, client, raw_api_key, db_session):
        from sqlalchemy import select
        from src.api.deps import require_api_key
        from src.api.main import app
        app.dependency_overrides.pop(require_api_key, None)
        await client.get("/api/v1/watches", headers={"X-API-Key": raw_api_key})
        key_hash = hashlib.sha256(raw_api_key.encode()).hexdigest()
        result = await db_session.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash)
        )
        key = result.scalar_one()
        assert key.last_used_at is not None
        app.dependency_overrides[require_api_key] = lambda: "test-user-id"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/api/test_api_key_auth.py -v -m integration 2>&1 | head -20
```
Expected: `ModuleNotFoundError` for `src.api.deps`

- [ ] **Step 3: Create `src/api/deps.py`**

```python
"""API authentication dependencies."""

import hashlib
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.core.models.api_key import ApiKey

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    raw_key: str | None = Depends(api_key_header),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """Validate X-API-Key header; return user_id on success.

    Raises 403 when header is absent, 401 when key is invalid or not found.
    Updates last_used_at on each successful authentication.
    """
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    api_key.last_used_at = datetime.now(UTC)
    await session.commit()
    return api_key.user_id
```

- [ ] **Step 4: Add `require_api_key` to `v1_router` in `src/api/main.py`**

Add import at top:
```python
from src.api.deps import require_api_key
```

Change:
```python
v1_router = APIRouter(prefix="/api/v1")
```
To:
```python
v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
```

- [ ] **Step 5: Add `require_api_key` override to `tests/conftest.py`**

Add import:
```python
from src.api.deps import require_api_key
```

In the `client` fixture, add:
```python
app.dependency_overrides[require_api_key] = lambda: "test-user-id"
```

- [ ] **Step 6: Run all non-integration tests to confirm no regressions**

```bash
uv run pytest --no-cov -m "not integration" -q
```
Expected: all pass (same count as before)

- [ ] **Step 7: Run auth tests**

```bash
uv run pytest tests/api/test_api_key_auth.py -v -m integration
```
Expected: 4 passed

- [ ] **Step 8: Commit**

```bash
git add src/api/deps.py src/api/main.py tests/conftest.py tests/api/test_api_key_auth.py
git commit -m "#100 feat: add require_api_key dep and gate /api/v1/ router"
```

---

## Task 6: Settings routes (`src/dashboard/settings.py`)

**Files:**
- Create: `src/dashboard/settings.py`
- Create: `tests/dashboard/test_settings_api_keys.py`

- [ ] **Step 1: Write failing route tests**

```python
# tests/dashboard/test_settings_api_keys.py
"""Integration tests for API key management settings routes."""

import hashlib

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.models.api_key import ApiKey
from src.core.models.app_user import AppUser
from src.dashboard.deps import generate_api_key

pytestmark = pytest.mark.integration

HTMX_HEADERS = {"HX-Request": "true"}


@pytest.fixture
async def make_api_key(db_session):
    """Factory: upsert test AppUser then insert an ApiKey; return the key row.

    AppUser must exist before ApiKey due to FK constraint. The conftest
    override_dashboard_user returns a bare AppUser instance without a DB
    upsert, so each test that needs a real api_keys row must seed the user here.
    """
    async def _make(label="My Key"):
        # Seed the AppUser row (upsert is safe when called multiple times)
        stmt = (
            pg_insert(AppUser)
            .values(id="test-user-id", email="test@example.com")
            .on_conflict_do_update(index_elements=["id"], set_={"email": "test@example.com"})
        )
        await db_session.execute(stmt)

        raw_key, key_hash, key_prefix = generate_api_key()
        key = ApiKey(
            user_id="test-user-id",
            label=label,
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        db_session.add(key)
        await db_session.flush()
        return key
    return _make


class TestSettingsLanding:
    async def test_settings_page_returns_200(self, client):
        r = await client.get("/settings")
        assert r.status_code == 200
        assert b"Settings" in r.content

    async def test_settings_page_has_api_keys_card(self, client):
        r = await client.get("/settings")
        assert b"API Keys" in r.content


class TestApiKeysList:
    async def test_list_page_returns_200(self, client):
        r = await client.get("/settings/api-keys")
        assert r.status_code == 200
        assert b"API Keys" in r.content

    async def test_list_shows_existing_key(self, client, make_api_key):
        await make_api_key("My Test Key")
        r = await client.get("/settings/api-keys")
        assert b"My Test Key" in r.content

    async def test_new_row_returns_form(self, client):
        r = await client.get("/settings/api-keys/new-row")
        assert r.status_code == 200
        assert b"label" in r.content.lower()


class TestApiKeysCreate:
    async def test_create_htmx_returns_modal_with_key(self, client):
        r = await client.post(
            "/settings/api-keys",
            data={"label": "New Key"},
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 200
        assert b"co_" in r.content
        assert b"not be shown again" in r.content.lower()

    async def test_create_non_htmx_redirects(self, client):
        r = await client.post("/settings/api-keys", data={"label": "New Key"})
        assert r.status_code in (200, 303)   # follows redirect to list

    async def test_create_empty_label_returns_422(self, client):
        r = await client.post(
            "/settings/api-keys",
            data={"label": "   "},
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 422


class TestApiKeysEdit:
    async def test_edit_row_get_returns_form(self, client, make_api_key):
        key = await make_api_key()
        r = await client.get(f"/settings/api-keys/{key.id}/edit-row")
        assert r.status_code == 200
        assert b"My Key" in r.content

    async def test_edit_row_post_saves_label(self, client, make_api_key):
        key = await make_api_key()
        r = await client.post(
            f"/settings/api-keys/{key.id}/edit-row",
            data={"label": "Renamed"},
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 200
        assert b"Renamed" in r.content

    async def test_edit_row_post_empty_label_returns_422(self, client, make_api_key):
        key = await make_api_key()
        r = await client.post(
            f"/settings/api-keys/{key.id}/edit-row",
            data={"label": "   "},
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 422

    async def test_read_row_returns_label(self, client, make_api_key):
        key = await make_api_key("Read Row Key")
        r = await client.get(f"/settings/api-keys/{key.id}/read-row")
        assert r.status_code == 200
        assert b"Read Row Key" in r.content

    async def test_edit_other_users_key_returns_404(self, client, db_session):
        """User isolation: cannot edit a key belonging to a different user."""
        from src.core.models.app_user import AppUser
        stmt = (
            pg_insert(AppUser)
            .values(id="other-user-id", email="other@example.com")
            .on_conflict_do_update(index_elements=["id"], set_={"email": "other@example.com"})
        )
        await db_session.execute(stmt)
        raw_key, key_hash, key_prefix = generate_api_key()
        other_key = ApiKey(
            user_id="other-user-id",
            label="Other Key",
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        db_session.add(other_key)
        await db_session.flush()

        r = await client.post(
            f"/settings/api-keys/{other_key.id}/edit-row",
            data={"label": "Hijacked"},
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 404


class TestApiKeysDelete:
    async def test_delete_removes_key(self, client, make_api_key, db_session):
        from sqlalchemy import select
        key = await make_api_key()
        r = await client.delete(
            f"/settings/api-keys/{key.id}",
            headers=HTMX_HEADERS,
        )
        assert r.status_code == 200
        result = await db_session.execute(select(ApiKey).where(ApiKey.id == key.id))
        assert result.scalar_one_or_none() is None

    async def test_delete_nonexistent_returns_404(self, client):
        r = await client.delete("/settings/api-keys/nonexistent", headers=HTMX_HEADERS)
        assert r.status_code == 404

    async def test_delete_other_users_key_returns_404(self, client, db_session):
        from src.core.models.app_user import AppUser
        stmt = (
            pg_insert(AppUser)
            .values(id="other-user-id", email="other@example.com")
            .on_conflict_do_update(index_elements=["id"], set_={"email": "other@example.com"})
        )
        await db_session.execute(stmt)
        raw_key, key_hash, key_prefix = generate_api_key()
        other_key = ApiKey(
            user_id="other-user-id",
            label="Other Key",
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
        db_session.add(other_key)
        await db_session.flush()

        r = await client.delete(f"/settings/api-keys/{other_key.id}", headers=HTMX_HEADERS)
        assert r.status_code == 404
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/dashboard/test_settings_api_keys.py -v -m integration 2>&1 | head -20
```
Expected: 404 Not Found (routes don't exist yet)

- [ ] **Step 3: Create `src/dashboard/settings.py`**

```python
"""Dashboard settings routes — API key management."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.core.models.api_key import ApiKey
from src.core.models.app_user import AppUser
from src.dashboard import templates
from src.dashboard.deps import generate_api_key, get_dashboard_user

router = APIRouter(prefix="/settings", tags=["settings"])


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"))


def _flash_trigger(level: str, body: str) -> dict[str, str]:
    import json
    return {"HX-Trigger": json.dumps({"showFlash": {"level": level, "body": body}})}


@router.get("")
async def settings_landing(
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id)
    )
    key_count = result.scalar_one()
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {"active_page": "settings", "user": user, "api_key_count": key_count},
    )


@router.get("/api-keys")
async def api_keys_list(
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user.id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "pages/settings_api_keys.html",
        {"active_page": "settings", "user": user, "keys": keys},
    )


@router.get("/api-keys/new-row")
async def api_key_new_row(
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
):
    return templates.TemplateResponse(
        request,
        "partials/api_key_edit_row.html",
        {"key": None},
    )


@router.post("/api-keys")
async def api_key_create(
    request: Request,
    label: str = Form(...),
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    label_val = label.strip()
    if not label_val:
        raise HTTPException(status_code=422, detail="label is required")
    raw_key, key_hash, key_prefix = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        label=label_val,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    session.add(key)
    await session.commit()
    if not _is_htmx(request):
        return RedirectResponse("/settings/api-keys", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/api_key_new_key_modal.html",
        {"raw_key": raw_key, "label": label_val},
    )


@router.get("/api-keys/{key_id}/edit-row")
async def api_key_edit_row_get(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/api_key_edit_row.html",
        {"key": key},
    )


@router.post("/api-keys/{key_id}/edit-row")
async def api_key_edit_row_post(
    key_id: str,
    request: Request,
    label: str = Form(...),
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    label_val = label.strip()
    if not label_val:
        raise HTTPException(status_code=422, detail="label is required")
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    key.label = label_val
    await session.commit()
    if not _is_htmx(request):
        return RedirectResponse("/settings/api-keys", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/api_key_row.html",
        {"key": key},
        headers=_flash_trigger("success", f"Key <strong>{escape(label_val)}</strong> renamed."),
    )


@router.get("/api-keys/{key_id}/read-row")
async def api_key_read_row(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/api_key_row.html", {"key": key})


@router.delete("/api-keys/{key_id}")
async def api_key_delete(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    label_val = key.label
    session.delete(key)
    await session.commit()
    return HTMLResponse(
        content="",
        status_code=200,
        headers=_flash_trigger("info", f"Key <strong>{escape(label_val)}</strong> deleted."),
    )
```

- [ ] **Step 4: Run tests — expect failures for missing templates**

```bash
uv run pytest tests/dashboard/test_settings_api_keys.py -v -m integration 2>&1 | head -30
```
Expected: `TemplateNotFound` errors — routes exist but templates don't yet.

- [ ] **Step 5: Commit routes (before templates)**

```bash
git add src/dashboard/settings.py tests/dashboard/test_settings_api_keys.py
git commit -m "#100 feat: add settings routes with API key CRUD"
```

---

## Task 7: Settings templates

**Files:**
- Create: `src/dashboard/templates/pages/settings.html`
- Create: `src/dashboard/templates/pages/settings_api_keys.html`
- Create: `src/dashboard/templates/partials/api_key_row.html`
- Create: `src/dashboard/templates/partials/api_key_edit_row.html`
- Create: `src/dashboard/templates/partials/api_key_new_key_modal.html`

Reference the existing `src/dashboard/templates/pages/notifications.html` and `src/dashboard/templates/partials/notification_template_row.html` for style conventions (`.data-table`, `.btn`, `.badge`, `.danger-zone`, flash OOB pattern).

- [ ] **Step 1: Create `src/dashboard/templates/pages/settings.html`**

```html
{% extends "base.html" %}
{% block title %}Settings — Watcher{% endblock %}

{% block content %}
<div class="max-w-4xl mx-auto px-4 py-6 space-y-6">
  <h1 class="text-2xl font-bold text-gray-900 dark:text-white">Settings</h1>

  <a href="/settings/api-keys"
     class="block p-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 shadow-sm hover:border-co-purple-600 dark:hover:border-co-purple-400 transition-colors">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-lg font-semibold text-gray-900 dark:text-white">API Keys</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Manage API keys for programmatic access via <code class="font-mono text-xs bg-gray-100 dark:bg-gray-700 px-1 rounded">X-API-Key</code>.
        </p>
      </div>
      <div class="text-right">
        <span class="text-2xl font-bold text-gray-900 dark:text-white">{{ api_key_count }}</span>
        <p class="text-xs text-gray-500 dark:text-gray-400">active</p>
      </div>
    </div>
  </a>
</div>
{% endblock %}
```

- [ ] **Step 2: Create `src/dashboard/templates/partials/api_key_row.html`**

```html
<tr id="api-key-row-{{ key.id }}">
  <td class="px-4 py-3 font-medium text-gray-900 dark:text-white">{{ key.label }}</td>
  <td class="px-4 py-3 font-mono text-sm text-gray-500 dark:text-gray-400">{{ key.key_prefix }}…</td>
  <td class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
    {{ key.created_at.strftime("%Y-%m-%d") if key.created_at else "—" }}
  </td>
  <td class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
    {{ key.last_used_at.strftime("%Y-%m-%d") if key.last_used_at else "Never" }}
  </td>
  <td class="px-4 py-3 text-end">
    <div class="flex justify-end gap-2">
      <button class="btn btn-secondary text-sm"
              hx-get="/settings/api-keys/{{ key.id }}/edit-row"
              hx-target="#api-key-row-{{ key.id }}"
              hx-swap="outerHTML">Edit</button>
      <button class="btn btn-danger text-sm"
              hx-delete="/settings/api-keys/{{ key.id }}"
              hx-target="#api-key-row-{{ key.id }}"
              hx-swap="outerHTML"
              hx-confirm="Delete key '{{ key.label }}'? This cannot be undone.">Delete</button>
    </div>
  </td>
</tr>
```

- [ ] **Step 3: Create `src/dashboard/templates/partials/api_key_edit_row.html`**

```html
{% set is_new = key is none %}
<tr id="{{ 'api-key-new-row' if is_new else 'api-key-row-' ~ key.id }}">
  <td colspan="5" class="px-4 py-3">
    <form class="flex items-center gap-3"
          {% if is_new %}
          hx-post="/settings/api-keys"
          hx-target="#api-keys-modal-container"
          hx-swap="innerHTML"
          {% else %}
          hx-post="/settings/api-keys/{{ key.id }}/edit-row"
          hx-target="#api-key-row-{{ key.id }}"
          hx-swap="outerHTML"
          {% endif %}
          aria-label="{{ 'Add new API key' if is_new else 'Edit API key label' }}">
      <input type="text"
             name="label"
             class="form-input flex-1"
             placeholder="Key label (e.g. Scripts, CI)"
             value="{{ key.label if key else '' }}"
             maxlength="100"
             required
             aria-label="Key label">
      <button type="submit" class="btn btn-primary text-sm">
        {{ "Generate" if is_new else "Save" }}
      </button>
      {% if is_new %}
      <button type="button" class="btn btn-secondary text-sm"
              onclick="this.closest('tr').remove()"
              aria-label="Cancel">Cancel</button>
      {% else %}
      <button type="button" class="btn btn-secondary text-sm"
              hx-get="/settings/api-keys/{{ key.id }}/read-row"
              hx-target="#api-key-row-{{ key.id }}"
              hx-swap="outerHTML">Cancel</button>
      {% endif %}
    </form>
  </td>
</tr>
```

- [ ] **Step 4: Create `src/dashboard/templates/partials/api_key_new_key_modal.html`**

```html
{# One-time key display — swap into #api-keys-modal-container; auto-shown via inline script #}
<div id="api-key-modal" role="dialog" aria-modal="true" aria-label="New API key generated"
     class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
  <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-lg p-6 space-y-4">
    <h2 class="text-lg font-semibold text-gray-900 dark:text-white">API key generated</h2>
    <p class="text-sm text-gray-500 dark:text-gray-400">
      <strong class="text-gray-900 dark:text-white">{{ label }}</strong> —
      copy this key now. <span class="text-red-600 dark:text-red-400 font-medium">It will not be shown again.</span>
    </p>
    <div class="flex gap-2">
      <input id="new-api-key-value"
             type="text"
             class="form-input flex-1 font-mono text-sm"
             value="{{ raw_key }}"
             readonly
             aria-label="New API key value">
      <button type="button"
              class="btn btn-secondary text-sm"
              onclick="navigator.clipboard.writeText(document.getElementById('new-api-key-value').value).then(()=>this.textContent='Copied!').catch(()=>{})"
              aria-label="Copy key to clipboard">Copy</button>
    </div>
    <div class="flex justify-end pt-2">
      <button type="button"
              class="btn btn-primary"
              onclick="location.reload();"
              aria-label="Close modal and refresh key list">Done</button>
    </div>
  </div>
</div>
```

- [ ] **Step 5: Create `src/dashboard/templates/pages/settings_api_keys.html`**

```html
{% extends "base.html" %}
{% block title %}API Keys — Settings — Watcher{% endblock %}

{% block content %}
<div class="max-w-5xl mx-auto px-4 py-6 space-y-6"
     aria-live="polite" aria-relevant="additions removals">

  <div class="flex items-center justify-between">
    <div>
      <a href="/settings" class="link text-sm">← Settings</a>
      <h1 class="text-2xl font-bold text-gray-900 dark:text-white mt-1">API Keys</h1>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
        Keys authenticate requests to <code class="font-mono text-xs bg-gray-100 dark:bg-gray-700 px-1 rounded">/api/v1/</code>
        via the <code class="font-mono text-xs bg-gray-100 dark:bg-gray-700 px-1 rounded">X-API-Key</code> header.
      </p>
    </div>
    <button class="btn btn-primary"
            hx-get="/settings/api-keys/new-row"
            hx-target="#api-keys-tbody"
            hx-swap="afterbegin"
            aria-label="Add new API key">
      + Generate new key
    </button>
  </div>

  {# Modal injection point #}
  <div id="api-keys-modal-container"></div>

  <div id="api-keys-table" class="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
    <table class="data-table w-full">
      <thead>
        <tr>
          <th scope="col" class="px-4 py-3 text-start">Label</th>
          <th scope="col" class="px-4 py-3 text-start">Prefix</th>
          <th scope="col" class="px-4 py-3 text-start">Created</th>
          <th scope="col" class="px-4 py-3 text-start">Last used</th>
          <th scope="col" class="px-4 py-3 text-end">Actions</th>
        </tr>
      </thead>
      <tbody id="api-keys-tbody">
        {% for key in keys %}
          {% include "partials/api_key_row.html" %}
        {% else %}
          <tr id="api-keys-empty-row">
            <td colspan="5" class="px-4 py-8 text-center text-sm text-gray-500 dark:text-gray-400">
              No API keys yet. Generate one to get started.
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="danger-zone rounded-lg border border-red-200 dark:border-red-800 p-4">
    <h2 class="text-sm font-semibold text-red-700 dark:text-red-400">Security reminder</h2>
    <p class="text-sm text-gray-600 dark:text-gray-400 mt-1">
      Keys grant full read/write access to the watcher API. Treat them like passwords.
      Delete keys you no longer use.
    </p>
  </div>
</div>
{% endblock %}
```

The create form POSTs to `/settings/api-keys` and swaps the response into `#api-keys-modal-container` (already set in `api_key_edit_row.html` above). The user clicks Done in the modal, which reloads the page to show the updated list.

- [ ] **Step 6: Run template tests**

```bash
uv run pytest tests/dashboard/test_settings_api_keys.py -v -m integration
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/
git commit -m "#100 feat: add settings templates (settings landing, API key CRUD)"
```

---

## Task 8: Register settings router + update nav

**Files:**
- Modify: `src/dashboard/__init__.py`
- Modify: `src/dashboard/templates/base.html`

- [ ] **Step 1: Add settings router to `src/dashboard/__init__.py`**

In `register_dashboard`, after the existing `from src.dashboard.routes import router` import, add:

```python
from src.dashboard.settings import router as settings_router
app.include_router(settings_router)
```

- [ ] **Step 2: Add Settings to nav in `base.html`**

Desktop nav (after the Notifications link, line ~32):
```html
<a href="/settings" class="nav-link {% if active_page == 'settings' %}nav-link-active{% endif %}">Settings</a>
```

Mobile nav (after the Notifications link, line ~74):
```html
<a href="/settings" class="nav-link {% if active_page == 'settings' %}nav-link-active{% endif %}">Settings</a>
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest --no-cov -m "not integration" -q
```
Expected: all pass

```bash
uv run pytest --no-cov -m integration -q 2>&1 | tail -5
```
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add src/dashboard/__init__.py src/dashboard/templates/base.html
git commit -m "#100 feat: register settings router and add Settings nav link"
```

---

## Task 9: Apply migration, make port public, final checks

- [ ] **Step 1: Confirm migration is applied**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
cd /home/exedev/watcher/.worktrees/api-key-management
uv run alembic current
```
Expected: shows `<rev> (head)`

- [ ] **Step 2: Merge worktree branch to main**

```bash
cd /home/exedev/watcher
git merge api-key-management --no-ff -m "#100 feat: API key management and public API access"
```

- [ ] **Step 3: Restart systemd service**

```bash
sudo systemctl restart watcher
sudo systemctl status watcher --no-pager
```
Expected: active (running)

- [ ] **Step 4: Make port 8000 public**

```bash
ssh exe.dev share set-public watcher
```

- [ ] **Step 5: Smoke test the live site**

```bash
# Dashboard still requires exe.dev auth (redirect)
curl -s -o /dev/null -w "%{http_code}" https://watcher.exe.xyz/
# Expected: 307

# API without key returns 403
curl -s -o /dev/null -w "%{http_code}" https://watcher.exe.xyz/api/v1/watches
# Expected: 403

# Health endpoint unaffected
curl -s https://watcher.exe.xyz/health
# Expected: {"status":"ok"}
```

- [ ] **Step 6: Close issue**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
gh issue close 100 --comment "Implemented. Migration applied, port 8000 made public via \`ssh exe.dev share set-public watcher\`."
```
