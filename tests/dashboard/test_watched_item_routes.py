"""Integration tests for WatchedItem dashboard routes."""

import pytest

pytestmark = pytest.mark.integration


class TestListPage:
    async def test_returns_200(self, client):
        response = await client.get("/watched-items")
        assert response.status_code == 200

    async def test_empty_state_renders_cta(self, client):
        response = await client.get("/watched-items")
        body = response.content
        # Empty state copy + CTA to /watches/new
        assert b"No watched items yet" in body
        assert b"/watches/new" in body

    async def test_list_renders_items(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        db_session.add(WatchedItem(info_item_id=item.info_item_id, name="Listed"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"Listed" in response.content

    async def test_sidebar_link_present(self, client):
        response = await client.get("/")
        assert b'href="/watched-items"' in response.content
