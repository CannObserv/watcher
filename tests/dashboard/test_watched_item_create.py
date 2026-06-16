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

    async def test_checked_checkbox_creates_active(self, client, db_session):
        """A checked checkbox submits is_active=true (the form's default state) → active."""
        await client.post(
            "/watched-items/new",
            data={"url": "https://example.com/active-default", "is_active": "true"},
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(
                    WatchedItem.effective_url == "https://example.com/active-default"
                )
            )
        ).scalar_one()
        assert wi.is_active is True

    async def test_provision_paused_when_is_active_unchecked(self, client, db_session):
        """Unchecking the box omits the field entirely (browsers don't submit unchecked
        checkboxes) → the item provisions paused (#190 fix)."""
        await client.post(
            "/watched-items/new",
            data={"url": "https://example.com/paused-omitted"},  # is_active field absent
            follow_redirects=False,
        )
        wi = (
            await db_session.execute(
                select(WatchedItem).where(
                    WatchedItem.effective_url == "https://example.com/paused-omitted"
                )
            )
        ).scalar_one()
        assert wi.is_active is False


class TestWatchedItemUrlReprobe:
    async def _make_wi(self, db_session, **kwargs):
        from tests.conftest import make_info_item

        item = await make_info_item(db_session, name=kwargs.pop("info_name", "ReprobeWI"))
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, **kwargs)
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        return wi

    async def test_reprobe_updates_url_and_domain(self, client, db_session):
        wi = await self._make_wi(
            db_session,
            name="UrlWI",
            effective_url="https://old.example/page",
            domain_name=None,
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/effective-url",
            data={"url": "https://new.example.org/fresh"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        await db_session.refresh(wi)
        assert wi.effective_url == "https://new.example.org/fresh"
        assert wi.domain_name == "new.example.org"

    async def test_reprobe_leaves_source_specs_untouched(self, client, db_session):
        wi = await self._make_wi(
            db_session,
            name="SpecsWI",
            effective_url="https://old.example/p",
            source_specs=[{"kind": "css", "selector": ".main"}],
        )
        await client.post(
            f"/watched-items/{wi.id}/effective-url",
            data={"url": "https://new.example.org/x"},
            follow_redirects=False,
        )
        await db_session.refresh(wi)
        assert wi.source_specs == [{"kind": "css", "selector": ".main"}]

    async def test_reprobe_archived_flashes_error(self, client, db_session):
        from datetime import UTC, datetime

        wi = await self._make_wi(
            db_session,
            name="ArchUrl",
            effective_url="https://x.example/p",
            archived_at=datetime.now(UTC),
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/effective-url",
            data={"url": "https://new.example.org/y"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"flash-error" in resp.content
        assert b"archived" in resp.content.lower()

    async def test_reprobe_unknown_returns_404(self, client):
        from ulid import ULID

        resp = await client.post(
            f"/watched-items/{ULID()}/effective-url",
            data={"url": "https://new.example.org/z"},
        )
        assert resp.status_code == 404

    async def test_reprobe_to_suspended_domain_warns_and_sets_suspended(self, client, db_session):
        """Re-probing onto a suspended domain re-evaluates domain_suspended and warns (#190 CR3)."""
        from src.core.models.domain import Domain

        # Pre-existing suspended (deactivated) domain.
        db_session.add(Domain(name="suspended.example", is_active=False))
        wi = await self._make_wi(
            db_session,
            name="ToSuspended",
            effective_url="https://ok.example/p",
            domain_suspended=False,
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/effective-url",
            data={"url": "https://suspended.example/page"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"flash-warning" in resp.content
        assert b"suspended" in resp.content.lower()
        await db_session.refresh(wi)
        assert wi.domain_name == "suspended.example"
        assert wi.domain_suspended is True

    async def test_reprobe_to_active_domain_clears_suspended(self, client, db_session):
        """Re-probing onto a healthy domain clears a stale domain_suspended flag."""
        wi = await self._make_wi(
            db_session,
            name="WasSuspended",
            effective_url="https://old.example/p",
            domain_suspended=True,
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/effective-url",
            data={"url": "https://fresh.example/page"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        await db_session.refresh(wi)
        assert wi.domain_suspended is False

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
