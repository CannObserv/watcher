"""Integration tests for audit log API endpoints."""

import pytest

from src.core.models.audit_log import EventType
from tests.conftest import bind_primary_source, make_info_item, make_info_source

pytestmark = pytest.mark.integration


async def _create_watch_via_api(client, db_session, *, name="W"):
    """Seed an InfoItem + bound primary InfoSource and create a Watch via the API.

    Exercises the full audit pipeline end-to-end.
    """
    item = await make_info_item(db_session, name=name)
    source = await make_info_source(db_session, url="https://example.com")
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=source.info_source_id,
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/watches",
        json={
            "name": name,
            "info_item_id": str(item.info_item_id),
            "content_type": "html",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestListAuditLog:
    async def test_list_audit_entries(self, client, db_session):
        await _create_watch_via_api(client, db_session, name="Audit Test")
        response = await client.get("/api/v1/audit")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(e["event_type"] == EventType.WATCH_CREATED for e in data)

    async def test_filter_by_event_type(self, client, db_session):
        await _create_watch_via_api(client, db_session, name="Event Filter")
        response = await client.get(f"/api/v1/audit?event_type={EventType.WATCH_CREATED}")
        assert response.status_code == 200
        assert all(e["event_type"] == EventType.WATCH_CREATED for e in response.json())

    async def test_filter_by_watch_id(self, client, db_session):
        watch_id = await _create_watch_via_api(client, db_session, name="Watch Filter")
        response = await client.get(f"/api/v1/audit?watch_id={watch_id}")
        assert response.status_code == 200
        assert all(e["watch_id"] == watch_id for e in response.json())

    async def test_pagination(self, client):
        response = await client.get("/api/v1/audit?limit=1")
        assert response.status_code == 200
        assert len(response.json()) <= 1


class TestWatchedItemEventTypes:
    def test_watched_item_event_constants_exist(self):
        from src.core.models.audit_log import EventType

        assert EventType.WATCHED_ITEM_UPDATED == "watched_item.updated"
        assert EventType.WATCHED_ITEM_ARCHIVED == "watched_item.archived"
        assert EventType.WATCHED_ITEM_RESTORED == "watched_item.restored"
        assert EventType.WATCHED_ITEM_REVIEWED == "watched_item.reviewed"
        assert EventType.WATCHED_ITEM_TEMPLATE_CREATED == "watched_item_template.created"
        assert EventType.WATCHED_ITEM_TEMPLATE_UPDATED == "watched_item_template.updated"
        assert EventType.WATCHED_ITEM_TEMPLATE_DELETED == "watched_item_template.deleted"


def test_watched_item_created_event_exists():
    assert EventType.WATCHED_ITEM_CREATED == "watched_item.created"
