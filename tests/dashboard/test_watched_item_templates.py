"""Integration tests for WatchedItem notification-template UI."""

import pytest

pytestmark = pytest.mark.integration


async def _seed(db_session, name="WI"):
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item

    item = await make_info_item(db_session)
    wi = WatchedItem(archiver_info_item_id=item.info_item_id, name=name)
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

    async def test_add_control_lives_in_partial(self, client, db_session):
        """#199 CR-2: the '+ Add' control moved into the partial's This Item header.

        It targets #wi-templates-tbody so it adds an item-owned template — keeping
        it in the page header dissociated it from its target and the new sections.
        """
        wi = await _seed(db_session)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert f"/watched-items/{wi.id}/templates/new".encode() in response.content
        assert b"wi-templates-tbody" in response.content


class TestInheritedNotificationSections:
    """#199: global + domain templates that fire for an item are surfaced read-only.

    These sources dispatch for the item (``dispatch_event_notifications``) but
    are not item-owned, so the panel shows them above the item-template table
    with no assign/remove — Edit/Test link back to the global library.
    """

    async def _global_tpl(self, db_session, title, *, is_global_default=True, is_active=True):
        from ulid import ULID

        from src.core.models.notification_template import NotificationTemplate

        tpl = NotificationTemplate(
            title=title,
            channel_hint="mailto",
            events=["change_detected"],
            is_global_default=is_global_default,
            is_active=is_active,
            remote_channel_id=str(ULID()),
        )
        db_session.add(tpl)
        await db_session.flush()
        await db_session.commit()
        return tpl

    async def test_global_section_lists_global_templates(self, client, db_session):
        wi = await _seed(db_session)
        await self._global_tpl(db_session, "GlobalEmailTpl")
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Global" in response.content
        assert b"GlobalEmailTpl" in response.content

    async def test_global_section_empty_state(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"No global templates" in response.content

    async def test_non_global_absent_from_global_section(self, client, db_session):
        wi = await _seed(db_session)
        await self._global_tpl(db_session, "NotGlobalTpl", is_global_default=False)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"NotGlobalTpl" not in response.content

    async def test_domain_section_lists_domain_templates(self, client, db_session):
        from src.core.models.notification_template import DomainNcRef
        from tests.conftest import make_watched_item

        wi = await make_watched_item(db_session, name="WI-Domain", domain_name="dom-nc.example.com")
        tpl = await self._global_tpl(db_session, "DomainSlackTpl", is_global_default=False)
        db_session.add(DomainNcRef(domain_name="dom-nc.example.com", template_id=tpl.id))
        await db_session.flush()
        await db_session.commit()

        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"DomainSlackTpl" in response.content

    async def test_domain_section_empty_state_when_no_domain(self, client, db_session):
        wi = await _seed(db_session)  # no domain_name
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"No domain notification defaults" in response.content

    async def test_full_page_renders_global_section(self, client, db_session):
        """SSR full page (not just the HTMX partial) includes the global section."""
        wi = await _seed(db_session)
        await self._global_tpl(db_session, "GlobalOnFullPage")
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"GlobalOnFullPage" in response.content


class TestTemplateCrudRoutes:
    async def test_new_form_renders(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/watched-items/{wi.id}/templates/new",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"channel_hint" in response.content
        # #190: event-name guidance for parity with the NotificationTemplate form
        assert b"Valid events" in response.content
        assert b"change_detected" in response.content

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
