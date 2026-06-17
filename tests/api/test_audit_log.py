"""Integration tests for audit log API endpoints."""

import pytest

from src.core.models.audit_log import EventType
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


async def _create_watched_item_via_api(client, db_session, *, name="W"):
    """Create a WatchedItem via the API; returns its id.

    The create route emits a ``WATCHED_ITEM_CREATED`` audit entry keyed by
    ``watched_item_id`` in the payload (#191 — the dedicated FK column is gone).
    """
    item = await make_info_item(db_session, name=name)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/watched-items",
        json={"archiver_info_item_id": str(item.info_item_id), "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestListAuditLog:
    async def test_list_audit_entries(self, client, db_session):
        await _create_watched_item_via_api(client, db_session, name="Audit Test")
        response = await client.get("/api/v1/audit")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(e["event_type"] == EventType.WATCHED_ITEM_CREATED for e in data)

    async def test_filter_by_event_type(self, client, db_session):
        await _create_watched_item_via_api(client, db_session, name="Event Filter")
        response = await client.get(f"/api/v1/audit?event_type={EventType.WATCHED_ITEM_CREATED}")
        assert response.status_code == 200
        assert all(e["event_type"] == EventType.WATCHED_ITEM_CREATED for e in response.json())

    async def test_filter_by_watched_item_id(self, client, db_session):
        wi_id = await _create_watched_item_via_api(client, db_session, name="WI Filter")
        response = await client.get(f"/api/v1/audit?watched_item_id={wi_id}")
        assert response.status_code == 200
        entries = response.json()
        assert entries
        assert all(e["payload"].get("watched_item_id") == wi_id for e in entries)

    async def test_pagination(self, client):
        response = await client.get("/api/v1/audit?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1

    async def test_filter_by_unknown_watched_item_id_returns_empty(self, client):
        """No ULID validation on the payload filter — unknown id just matches nothing."""
        response = await client.get("/api/v1/audit?watched_item_id=not-a-ulid")
        assert response.status_code == 200
        assert response.json() == []


class TestWatchedItemEventTypes:
    def test_watched_item_event_constants_exist(self):
        from src.core.models.audit_log import EventType

        assert EventType.WATCHED_ITEM_UPDATED == "watched_item.updated"
        assert EventType.WATCHED_ITEM_ARCHIVED == "watched_item.archived"
        assert EventType.WATCHED_ITEM_RESTORED == "watched_item.restored"
        assert EventType.WATCHED_ITEM_PAUSED == "watched_item.paused"
        assert EventType.WATCHED_ITEM_RESUMED == "watched_item.resumed"
        assert EventType.WATCHED_ITEM_REVIEWED == "watched_item.reviewed"
        assert EventType.WATCHED_ITEM_TEMPLATE_CREATED == "watched_item_template.created"
        assert EventType.WATCHED_ITEM_TEMPLATE_UPDATED == "watched_item_template.updated"
        assert EventType.WATCHED_ITEM_TEMPLATE_DELETED == "watched_item_template.deleted"


def test_watched_item_created_event_exists():
    assert EventType.WATCHED_ITEM_CREATED == "watched_item.created"
