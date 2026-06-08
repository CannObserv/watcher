"""Integration tests for standalone WatchedItem-create flow (#185 Phase A step 7)."""

import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.watched_item import WatchedItem

pytestmark = pytest.mark.integration


class TestWatchedItemCreateForm:
    async def test_form_returns_200(self, client):
        response = await client.get("/watched-items/new")
        assert response.status_code == 200
        assert b"New Watched Item" in response.content

    async def test_form_has_url_field(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'name="url"' in body

    async def test_form_has_default_fields(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'name="name"' in body
        assert b'name="description"' in body
        assert b'name="default_schedule_interval"' in body
        assert b'name="default_content_type"' in body
        assert b'name="default_tags"' in body

    async def test_form_lists_all_content_types(self, client):
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'value="html"' in body
        assert b'value="pdf"' in body
        assert b'value="file"' in body

    async def test_form_no_typeahead_picker(self, client):
        """Typeahead InfoItem picker has been removed (#185 Phase A step 7)."""
        response = await client.get("/watched-items/new")
        body = response.content
        assert b'role="combobox"' not in body
        assert b"info-items/search" not in body


class TestWatchedItemCreateSubmit:
    async def test_redirects_on_success(self, client, db_session):
        response = await client.post(
            "/watched-items/new",
            data={"url": "https://example.com/page", "name": "WI X"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/watched-items/")

    async def test_persists_defaults(self, client, db_session):
        await client.post(
            "/watched-items/new",
            data={
                "url": "https://example.com/defaults",
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
                select(WatchedItem).where(
                    WatchedItem.effective_url == "https://example.com/defaults"
                )
            )
        ).scalar_one()
        assert wi.name == "WI Y"
        assert wi.description == "note"
        assert wi.default_schedule_config == {"interval": "15m"}
        assert wi.default_content_type == "html"
        assert set(wi.default_tags) == {"regulatory", "legislative"}

    async def test_name_falls_back_to_domain(self, client, db_session):
        await client.post(
            "/watched-items/new",
            data={"url": "https://lcb.wa.gov/page"},
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.effective_url == "https://lcb.wa.gov/page")
            )
        ).scalar_one()
        assert wi.name == "lcb.wa.gov"

    async def test_sets_effective_url(self, client, db_session):
        await client.post(
            "/watched-items/new",
            data={"url": "https://example.com/wi-url"},
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.effective_url == "https://example.com/wi-url")
            )
        ).scalar_one()
        assert wi.effective_url == "https://example.com/wi-url"
        assert wi.domain_name == "example.com"

    async def test_missing_url_shows_flash(self, client):
        response = await client.post("/watched-items/new", data={"name": "X"})
        assert response.status_code == 200
        assert b"required" in response.content.lower()

    async def test_bad_interval_shows_flash(self, client):
        response = await client.post(
            "/watched-items/new",
            data={
                "url": "https://example.com/interval",
                "default_schedule_interval": "not-a-duration",
            },
        )
        assert response.status_code == 200
        assert b"interval" in response.content.lower()

    async def test_invalid_content_type_shows_flash(self, client):
        response = await client.post(
            "/watched-items/new",
            data={
                "url": "https://example.com/ct",
                "default_content_type": "garbage",
            },
        )
        assert response.status_code == 200
        assert b"content type" in response.content.lower()

    async def test_tag_too_long_shows_flash(self, client):
        response = await client.post(
            "/watched-items/new",
            data={
                "url": "https://example.com/tags",
                "default_tags": "x" * 256,
            },
        )
        assert response.status_code == 200
        assert b"too long" in response.content.lower()

    async def test_emits_audit_with_source_dashboard(self, client, db_session):
        await client.post(
            "/watched-items/new",
            data={"url": "https://example.com/audit"},
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
        assert b"New Watched Item" in body
