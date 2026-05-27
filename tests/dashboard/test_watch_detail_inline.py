"""Tests for watch detail inline field editing, status toggle, and danger zone."""

import pytest

from src.core.models.domain import Domain
from src.core.models.watch import ContentType
from tests.conftest import make_watch

pytestmark = pytest.mark.integration


class TestWatchDetailPage:
    """GET /watches/{id} — detail page renders inline-editable fields."""

    async def test_detail_page_has_status_toggle(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Toggle Test",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        assert b"watch-status-toggle" in response.content
        assert b"Active" in response.content

    async def test_detail_page_fields_show_edit_button_in_view_mode(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="View Mode",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        # Watch fields in view mode render plain text + Edit button, not disabled inputs.
        # (Unrelated disabled elements may exist elsewhere on the page, e.g. the
        # "Watch Created" checkbox in the notification add-form is intentionally disabled.)
        assert b"Edit" in response.content

    async def test_detail_page_shows_content_type_readonly(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="CT Read",
            primary_url="https://example.com",
            content_type=ContentType.PDF,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        assert b"PDF" in response.content
        assert b"Content Type" in response.content

    async def test_detail_page_no_fetch_config_fields(self, client, db_session):
        """Phase 2c: fetch_config keys live on the InfoSpec; the watch detail
        page no longer renders CSS selectors / chunk-row-size / etc."""
        watch = await make_watch(
            db_session,
            name="No Fetch Config",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "CSS Selectors" not in content
        assert "Strip Boilerplate" not in content
        assert "Skip Empty Pages" not in content
        assert "File Format" not in content
        assert "Chunk Row Size" not in content

    async def test_detail_page_has_danger_zone(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="DZ Test",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Danger Zone" in content
        assert "Archive" in content

    async def test_detail_page_has_metadata_footer(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Meta Test",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Metadata" in content
        assert str(watch.id) in content

    async def test_detail_page_no_edit_button_in_header(self, client, db_session):
        """Edit button in header removed — editing is now inline."""
        watch = await make_watch(
            db_session,
            name="No Edit Btn",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert f"/watches/{watch.id}/edit" not in content


class TestWatchFieldPartialGet:
    """GET /watches/{id}/field/{field} — serves field partial."""

    async def test_field_partial_view_mode_default(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Field View",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(
            f"/watches/{watch.id}/field/name",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Field View" in response.content
        assert b"<input" not in response.content
        assert b"Edit" in response.content

    async def test_field_partial_edit_mode(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Field Edit",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(
            f"/watches/{watch.id}/field/name?mode=edit",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"disabled" not in response.content
        assert b"Save" in response.content
        assert b"Cancel" in response.content

    async def test_field_partial_invalid_field_returns_400(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Bad Field",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}/field/nonexistent")
        assert response.status_code == 400

    async def test_field_partial_nonexistent_watch_returns_404(self, client):
        response = await client.get(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/field/name",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 404

    async def test_field_partial_non_htmx_redirects(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="No HTMX",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(
            f"/watches/{watch.id}/field/name",
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestWatchFieldUpdate:
    """POST /watches/{id}/field/{field} — updates a single field."""

    async def test_update_name_returns_view_mode(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Old Name",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/field/name",
            data={"value": "New Name"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"New Name" in response.content
        assert b"<input" not in response.content

    async def test_interval_field_inline_edit_returns_400(self, client, db_session):
        """#160 Task 11.3: schedule_config moved to the WatchedItem; per-Watch
        interval edit is gone. The dispatcher must reject the legacy field name.
        """
        watch = await make_watch(
            db_session,
            name="Interval",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/field/interval",
            data={"value": "6h"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    async def test_update_url_field_returns_400(self, client, db_session):
        """Phase 2c: ``url`` is no longer an editable column; the dispatcher rejects."""
        watch = await make_watch(
            db_session,
            name="No URL",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/field/url",
            data={"value": "https://new.com"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    async def test_update_fetch_config_field_returns_400(self, client, db_session):
        """Phase 2c: fetch_config keys live on the InfoSpec, not on the Watch."""
        watch = await make_watch(
            db_session,
            name="No fetch_config",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/field/timeout",
            data={"value": "60"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    async def test_update_invalid_field_returns_400(self, client, db_session):
        watch = await make_watch(
            db_session, name="Bad", primary_url="https://example.com", content_type=ContentType.HTML
        )
        response = await client.post(
            f"/watches/{watch.id}/field/nonexistent",
            data={"value": "x"},
        )
        assert response.status_code == 400

    async def test_non_htmx_redirects(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Redirect",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/field/name",
            data={"value": "Updated"},
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestWatchDetailDomainRow:
    """GET /watches/{id} — Domain row in Details section."""

    async def test_detail_page_shows_domain_link(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Domain Row",
            primary_url="https://domain-row.com/p",
            content_type=ContentType.HTML,
            domain_name="domain-row.com",
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        assert b"/domains/domain-row.com" in response.content

    async def test_detail_page_domain_row_label(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Domain Label",
            primary_url="https://domain-label.com/p",
            content_type=ContentType.HTML,
            domain_name="domain-label.com",
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Domain" in content
        assert "domain-label.com" in content

    async def test_detail_page_no_domain_row_when_no_effective_domain(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="No Domain",
            primary_url="https://nodomain.com/p",
            content_type=ContentType.HTML,
            domain_name=None,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert b"/domains/" not in response.content


class TestWatchStatusToggle:
    """POST /watches/{id}/toggle-active — toggles active status."""

    async def test_toggle_active_to_inactive(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Active",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=True,
        )
        response = await client.post(
            f"/watches/{watch.id}/toggle-active",
            data={"active": ""},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Inactive" in response.content

    async def test_domain_suspended_watch_detail_shows_domain_inactive_badge(
        self, client, db_session
    ):
        watch = await make_watch(
            db_session,
            name="Suspended Badge",
            primary_url="https://susp-badge.com/p",
            content_type=ContentType.HTML,
            is_active=False,
            domain_suspended=True,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert b"Domain Inactive" in response.content

    async def test_manually_inactive_watch_detail_shows_inactive_not_domain_inactive(
        self, client, db_session
    ):
        watch = await make_watch(
            db_session,
            name="Manual Badge",
            primary_url="https://manual-badge.com/p",
            content_type=ContentType.HTML,
            is_active=False,
            domain_suspended=False,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Domain Inactive" not in content
        assert "Inactive" in content

    async def test_toggle_inactive_to_active(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Inactive",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
        )
        response = await client.post(
            f"/watches/{watch.id}/toggle-active",
            data={"active": "true"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Active" in response.content

    async def test_toggle_archived_returns_409(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Archived",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
            is_archived=True,
        )
        response = await client.post(
            f"/watches/{watch.id}/toggle-active",
            data={"active": "true"},
        )
        assert response.status_code == 409

    async def test_toggle_activate_blocked_when_domain_inactive(self, client, db_session):
        db_session.add(Domain(name="inactive-domain.com", is_active=False))
        watch = await make_watch(
            db_session,
            name="Suspended Watch",
            primary_url="https://inactive-domain.com/p",
            content_type=ContentType.HTML,
            domain_name="inactive-domain.com",
            is_active=False,
            domain_suspended=True,
        )
        response = await client.post(
            f"/watches/{watch.id}/toggle-active",
            data={"active": "true"},
        )
        assert response.status_code == 409

    async def test_toggle_deactivate_allowed_when_domain_inactive(self, client, db_session):
        """Deactivating is always allowed; only activation is blocked."""
        db_session.add(Domain(name="inactive-domain2.com", is_active=False))
        watch = await make_watch(
            db_session,
            name="Active Despite Inactive Domain",
            primary_url="https://inactive-domain2.com/p",
            content_type=ContentType.HTML,
            domain_name="inactive-domain2.com",
            is_active=True,
        )
        response = await client.post(
            f"/watches/{watch.id}/toggle-active",
            data={"active": ""},
        )
        assert response.status_code in (200, 303)
        await db_session.refresh(watch)
        assert watch.is_active is False
        assert watch.domain_suspended is False


class TestWatchArchiveRestore:
    """POST /watches/{id}/archive and /restore — archive/restore workflow."""

    async def test_archive_sets_flags(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="To Archive",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.post(
            f"/watches/{watch.id}/archive",
            follow_redirects=False,
        )
        assert response.status_code == 303
        await db_session.refresh(watch)
        assert watch.is_archived is True
        assert watch.is_active is False

    async def test_restore_clears_archived_stays_inactive(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="To Restore",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
            is_archived=True,
        )
        response = await client.post(
            f"/watches/{watch.id}/restore",
            follow_redirects=False,
        )
        assert response.status_code == 303
        await db_session.refresh(watch)
        assert watch.is_archived is False
        assert watch.is_active is False

    async def test_archived_detail_shows_delete_button(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Archived DZ",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
            is_archived=True,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Delete permanently" in content
        assert "Restore" in content

    async def test_non_archived_detail_shows_archive_button(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Active DZ",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        content = response.content.decode()
        assert "Archive" in content
        assert "Delete permanently" not in content


class TestWatchDeleteRequiresArchived:
    """DELETE /watches/{id} — requires archived status."""

    async def test_delete_non_archived_returns_409(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Not Archived",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
        )
        response = await client.delete(f"/watches/{watch.id}")
        assert response.status_code == 409

    async def test_delete_archived_succeeds(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Archived Del",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_active=False,
            is_archived=True,
        )
        watch_id = str(watch.id)
        response = await client.delete(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert response.headers.get("HX-Redirect") == "/watches"
