"""Integration tests for standalone WatchedItem-create flow (#162)."""

import pytest
from sqlalchemy import select

from src.core.models.watched_item import WatchedItem
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


class TestWatchedItemCreateForm:
    async def test_form_returns_200(self, client):
        response = await client.get("/watched-items/new")
        assert response.status_code == 200
        assert b"New WatchedItem" in response.content

    async def test_form_renders_typeahead_picker(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'role="combobox"' in body
        # The form's picker uses select_only mode (no sub_aspect picking)
        assert b"select_only" in body

    async def test_form_has_default_fields(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'name="name"' in body
        assert b'name="description"' in body
        assert b'name="default_schedule_interval"' in body
        assert b'name="default_content_type"' in body
        assert b'name="default_tags"' in body


class TestWatchedItemCreateSubmit:
    async def test_redirects_on_success(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Pre-Bound")
        await db_session.commit()
        response = await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id), "name": "WI X"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/watched-items/")

    async def test_persists_defaults(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Pre-Bound 2")
        await db_session.commit()
        await client.post(
            "/watched-items/new",
            data={
                "info_item_id": str(item.info_item_id),
                "name": "WI Y",
                "description": "note",
                "default_schedule_interval": "15m",
                "default_content_type": "html",
                "default_tags": "regulatory, legislative",
            },
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.info_item_id == item.info_item_id)
            )
        ).scalar_one()
        assert wi.name == "WI Y"
        assert wi.description == "note"
        assert wi.default_schedule_config == {"interval": "15m"}
        assert wi.default_content_type == "html"
        assert set(wi.default_tags) == {"regulatory", "legislative"}

    async def test_name_falls_back_to_info_item_name(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source Item")
        await db_session.commit()
        await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id)},
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.info_item_id == item.info_item_id)
            )
        ).scalar_one()
        assert wi.name == "Source Item"

    async def test_duplicate_info_item_shows_flash(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="X")
        db_session.add(WatchedItem(info_item_id=item.info_item_id, name="exists"))
        await db_session.commit()
        response = await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 200
        assert b"already exists" in response.content

    async def test_missing_info_item_id_shows_flash(self, client):
        response = await client.post("/watched-items/new", data={"name": "X"})
        assert response.status_code == 200
        assert b"required" in response.content.lower()

    async def test_bad_interval_shows_flash(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Y")
        await db_session.commit()
        response = await client.post(
            "/watched-items/new",
            data={
                "info_item_id": str(item.info_item_id),
                "default_schedule_interval": "not-a-duration",
            },
        )
        assert response.status_code == 200
        assert b"interval" in response.content.lower()

    async def test_emits_audit_with_source_dashboard(self, client, db_session, info_client):
        from src.core.models.audit_log import AuditLog, EventType

        item = await make_info_item(db_session, name="Z")
        await db_session.commit()
        await client.post(
            "/watched-items/new",
            data={"info_item_id": str(item.info_item_id)},
        )
        events = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1, f"expected 1 audit row, got {len(events)}"
        assert events[0].payload["source"] == "dashboard"


class TestListPageHasCreateLink:
    async def test_list_page_has_new_button(self, client):
        response = await client.get("/watched-items")
        body = response.content
        assert b"/watched-items/new" in body
        assert b"New WatchedItem" in body
