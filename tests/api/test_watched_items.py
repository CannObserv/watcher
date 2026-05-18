"""Integration tests for WatchedItem API endpoints."""

from unittest.mock import AsyncMock

import pytest
from archiver_client import NotFound
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


async def _make_watched_item(db_session, **overrides):
    """Helper: create a WatchedItem + parent InfoItem via the test fixtures."""
    from src.core.models.watched_item import WatchedItem

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


class TestArchiveRestore:
    async def test_archive_marks_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        data = response.json()
        assert data["archived_at"] is not None
        assert data["is_active"] is False

    async def test_archive_cascades_to_child_watches(self, client, db_session):
        from tests.conftest import make_watch

        wi = await _make_watched_item(db_session)
        w1 = await make_watch(db_session, name="C1", watched_item=wi)
        w2 = await make_watch(db_session, name="C2", watched_item=wi)
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        # Reload children and confirm cascade
        await db_session.refresh(w1)
        await db_session.refresh(w2)
        assert w1.is_active is False and w1.is_archived is True
        assert w2.is_active is False and w2.is_archived is True

    async def test_restore_parent_only(self, client, db_session):
        from datetime import UTC, datetime

        from tests.conftest import make_watch

        wi = await _make_watched_item(db_session, archived_at=datetime.now(UTC), is_active=False)
        w = await make_watch(
            db_session,
            name="ChildArchived",
            watched_item=wi,
            is_active=False,
            is_archived=True,
        )
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")
        assert response.status_code == 200
        assert response.json()["archived_at"] is None
        await db_session.refresh(w)
        # Restore is parent-only — children stay archived.
        assert w.is_archived is True

    async def test_archive_404(self, client):
        from ulid import ULID

        response = await client.post(f"/api/v1/watched-items/{ULID()}/archive")
        assert response.status_code == 404


class TestMarkReviewed:
    async def test_stamps_now(self, client, db_session):
        wi = await _make_watched_item(db_session)
        before = wi.last_reviewed_at
        response = await client.post(f"/api/v1/watched-items/{wi.id}/mark-reviewed")
        assert response.status_code == 200
        stamped = response.json()["last_reviewed_at"]
        assert stamped is not None
        assert before is None or stamped > before.isoformat()

    async def test_404(self, client):
        from ulid import ULID

        response = await client.post(f"/api/v1/watched-items/{ULID()}/mark-reviewed")
        assert response.status_code == 404


class TestTemplateCrud:
    async def test_list_empty(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.get(f"/api/v1/watched-items/{wi.id}/notification-templates")
        assert response.status_code == 200
        assert response.json() == []

    async def test_create_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={
                "title": "Email Greg",
                "channel_hint": "mailto://x:y@z",
                "events": ["change_detected"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Email Greg"
        assert data["watched_item_id"] == str(wi.id)

    async def test_create_404_unknown_parent(self, client):
        from ulid import ULID

        response = await client.post(
            f"/api/v1/watched-items/{ULID()}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        assert response.status_code == 404

    async def test_patch_updates(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}",
            json={"is_active": False, "title": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert response.json()["title"] == "Renamed"

    async def test_delete(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.delete(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}"
        )
        assert response.status_code == 204
        # Verify gone
        listing = await client.get(f"/api/v1/watched-items/{wi.id}/notification-templates")
        assert listing.json() == []


class TestCreateWatchedItem:
    async def test_creates_with_info_item_name_fallback(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source Item")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={"info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["info_item_id"] == str(item.info_item_id)
        # Name falls back to the InfoItem's name when not supplied.
        assert body["name"] == "Source Item"
        assert body["default_schedule_config"] is None
        assert body["archived_at"] is None

    async def test_uses_supplied_name(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "info_item_id": str(item.info_item_id),
                "name": "Overridden",
                "default_schedule_config": {"interval": "10m"},
                "default_tags": ["regulatory"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Overridden"
        assert body["default_schedule_config"] == {"interval": "10m"}
        assert body["default_tags"] == ["regulatory"]

    async def test_duplicate_info_item_id_returns_409(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="X")
        await db_session.commit()
        r1 = await client.post(
            "/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)}
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)}
        )
        assert r2.status_code == 409
        assert "already" in r2.json()["detail"].lower()

    async def test_unknown_info_item_returns_422(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.post(
            "/api/v1/watched-items",
            json={"info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ"},
        )
        assert response.status_code == 422

    async def test_emits_audit_event(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="A")
        await db_session.commit()
        await client.post("/api/v1/watched-items", json={"info_item_id": str(item.info_item_id)})
        events = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["source"] == "api"
