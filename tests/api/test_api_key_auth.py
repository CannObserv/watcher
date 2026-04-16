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
    db_session.add(
        ApiKey(
            user_id="test-user-id",
            label="test key",
            key_prefix=key_prefix,
            key_hash=key_hash,
        )
    )
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
        result = await db_session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
        key = result.scalar_one()
        assert key.last_used_at is not None
        app.dependency_overrides[require_api_key] = lambda: "test-user-id"
