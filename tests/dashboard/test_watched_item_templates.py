"""Integration tests for WatchedItem notification-template UI."""

import pytest

pytestmark = pytest.mark.integration


async def _seed(db_session, name="WI"):
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item

    item = await make_info_item(db_session)
    wi = WatchedItem(info_item_id=item.info_item_id, name=name)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


async def _seed_tpl(db_session, watched_item):
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )

    tpl = WatchedItemNotificationTemplate(
        watched_item_id=watched_item.id,
        title="Email",
        channel_hint="mailto://x:y@z",
    )
    db_session.add(tpl)
    await db_session.flush()
    await db_session.commit()
    return tpl


class TestTemplatesPartial:
    async def test_list_empty(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"No notification templates" in response.content

    async def test_list_renders_row(self, client, db_session):
        wi = await _seed(db_session)
        await _seed_tpl(db_session, wi)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"Email" in response.content
        assert b"mailto" in response.content or b"channel" in response.content


class TestTemplateCrudRoutes:
    async def test_new_form_renders(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/watched-items/{wi.id}/templates/new",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"channel_hint" in response.content

    async def test_create_inserts_row(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.post(
            f"/watched-items/{wi.id}/templates",
            data={"title": "T1", "channel_hint": "mailto://a:b@c", "events": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        listing = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"T1" in listing.content

    async def test_edit_form_renders(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.get(
            f"/watched-items/{wi.id}/templates/{tpl.id}/edit",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Email" in response.content

    async def test_update_persists(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.post(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            data={
                "title": "Renamed",
                "channel_hint": tpl.channel_hint,
                "events": "change_detected",
            },
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(tpl)
        assert tpl.title == "Renamed"

    async def test_delete_removes_row(self, client, db_session):
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.delete(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        listing = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"No notification templates" in listing.content

    async def test_create_returns_rows_only_not_wrapped_table(self, client, db_session):
        """Regression: mutation handlers must return rows-only to avoid nested-table HTML.

        hx-target is #wi-templates-tbody with innerHTML swap, so the response body
        must be tbody-row content, NOT a wrapped <table>.
        """
        wi = await _seed(db_session)
        response = await client.post(
            f"/watched-items/{wi.id}/templates",
            data={"title": "T", "channel_hint": "mailto://a:b@c", "events": "change_detected"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        body = response.content
        # Must NOT contain a wrapping <table> or <thead>
        assert b"<table" not in body
        assert b"<thead" not in body
        # SHOULD contain a row with the new title
        assert b"<tr" in body
        assert b"T" in body

    async def test_update_returns_rows_only_not_wrapped_table(self, client, db_session):
        """Regression: update mutation must return rows-only partial, not wrapped table."""
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.post(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            data={
                "title": "Renamed",
                "channel_hint": tpl.channel_hint,
                "events": "change_detected",
            },
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        body = response.content
        assert b"<table" not in body
        assert b"<thead" not in body
        assert b"<tr" in body
        assert b"Renamed" in body

    async def test_delete_returns_rows_only_not_wrapped_table(self, client, db_session):
        """Regression: delete mutation must return rows-only partial, not wrapped table."""
        wi = await _seed(db_session)
        tpl = await _seed_tpl(db_session, wi)
        response = await client.delete(
            f"/watched-items/{wi.id}/templates/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        body = response.content
        assert b"<table" not in body
        assert b"<thead" not in body
        # After deleting the only template, the empty-state row should appear
        assert b"<tr" in body
        assert b"No notification templates" in body
