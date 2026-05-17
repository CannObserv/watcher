"""Integration tests for WatchedItem API endpoints."""

import pytest

pytestmark = pytest.mark.integration


async def _make_watched_item(db_session, **overrides):
    """Helper: create a WatchedItem + parent InfoItem via the test fixtures."""
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item

    item = await make_info_item(db_session)
    wi = WatchedItem(info_item_id=item.info_item_id, name=overrides.pop("name", "Test WI"))
    for k, v in overrides.items():
        setattr(wi, k, v)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestListWatchedItems:
    async def test_empty_list(self, client):
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_returns_items(self, client, db_session):
        await _make_watched_item(db_session, name="Alpha")
        await _make_watched_item(db_session, name="Beta")
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert {"Alpha", "Beta"} <= set(names)

    async def test_archived_excluded_by_default(self, client, db_session):
        from datetime import UTC, datetime

        await _make_watched_item(db_session, name="Active")
        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items")
        names = [r["name"] for r in response.json()]
        assert "Active" in names
        assert "Archived" not in names

    async def test_archived_included_when_requested(self, client, db_session):
        from datetime import UTC, datetime

        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items?include_archived=true")
        names = [r["name"] for r in response.json()]
        assert "Archived" in names


class TestGetWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.get(f"/api/v1/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Single")
        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Single"


class TestPatchWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.patch(f"/api/v1/watched-items/{ULID()}", json={"name": "x"})
        assert response.status_code == 404

    async def test_rename(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Old")
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"name": "New"})
        assert response.status_code == 200
        assert response.json()["name"] == "New"

    async def test_update_schedule(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "30m"}},
        )
        assert response.status_code == 200
        assert response.json()["default_schedule_config"] == {"interval": "30m"}

    async def test_update_tags(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"default_tags": ["a", "b"]}
        )
        assert response.json()["default_tags"] == ["a", "b"]

    async def test_empty_patch_is_noop(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Stays")
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={})
        assert response.status_code == 200
        assert response.json()["name"] == "Stays"

    async def test_invalid_content_type(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_content_type": "bogus"},
        )
        assert response.status_code == 422
