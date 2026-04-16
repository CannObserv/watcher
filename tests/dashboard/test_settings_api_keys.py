"""Integration tests for API key management settings routes."""

import pytest
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
        assert r.status_code in (200, 303)  # follows redirect to list

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
