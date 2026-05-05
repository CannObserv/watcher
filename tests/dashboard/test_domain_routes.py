"""Integration tests for domain dashboard routes."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.domain import Domain
from tests.conftest import make_watch

pytestmark = pytest.mark.integration


class TestDomainsListPage:
    async def test_domains_page_returns_200(self, client):
        response = await client.get("/domains")
        assert response.status_code == 200
        assert b"Domains" in response.content

    async def test_domains_page_has_create_link(self, client):
        response = await client.get("/domains")
        assert b"/domains/new" in response.content

    async def test_domains_page_has_search_input(self, client):
        response = await client.get("/domains")
        assert b'name="q"' in response.content

    async def test_domains_page_has_filter_pills(self, client):
        response = await client.get("/domains")
        assert b"Active" in response.content
        assert b"Archived" in response.content

    async def test_domains_table_partial(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200

    async def test_domains_table_search(self, client, db_session):
        db_session.add(Domain(name="findme.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()
        response = await client.get("/partials/domains-table?q=findme&status=")
        assert response.status_code == 200
        assert b"findme.com" in response.content
        assert b"other.com" not in response.content

    async def test_domains_table_has_edit_button(self, client, db_session):
        db_session.add(Domain(name="editable.com"))
        await db_session.flush()
        response = await client.get("/partials/domains-table?status=")
        assert b"/domains/editable.com" in response.content

    async def test_domains_table_shows_last_checked(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200
        assert b"Last Checked" in response.content


class TestDomainCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/domains/new")
        assert response.status_code == 200
        assert b"New Domain" in response.content

    async def test_create_form_has_url_input(self, client):
        response = await client.get("/domains/new")
        assert b'name="url"' in response.content

    async def test_create_domain_redirects_to_detail(self, client):
        response = await client.post(
            "/domains",
            data={"url": "https://newdomain.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "newdomain.com" in response.headers["location"]

    async def test_create_domain_missing_url_shows_error(self, client):
        response = await client.post("/domains", data={"url": ""})
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()

    async def test_create_domain_duplicate_redirects_to_existing(self, client, db_session):
        db_session.add(Domain(name="existing.com"))
        await db_session.flush()
        response = await client.post(
            "/domains",
            data={"url": "https://existing.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "existing.com" in response.headers["location"]


class TestDomainDetail:
    async def test_detail_page_returns_200(self, client, db_session):
        db_session.add(Domain(name="detail.com"))
        await db_session.flush()
        response = await client.get("/domains/detail.com")
        assert response.status_code == 200
        assert b"detail.com" in response.content

    async def test_detail_page_404_nonexistent(self, client):
        response = await client.get("/domains/nonexistent.com")
        assert response.status_code == 404

    async def test_detail_page_shows_config_fields(self, client, db_session):
        db_session.add(Domain(name="config.com", min_interval=3.5, max_concurrency=5))
        await db_session.flush()
        response = await client.get("/domains/config.com")
        assert b"3.5" in response.content
        assert b"Minimum seconds between requests" in response.content

    async def test_detail_page_shows_notes(self, client, db_session):
        db_session.add(Domain(name="noted.com", notes="Important note"))
        await db_session.flush()
        response = await client.get("/domains/noted.com")
        assert b"Important note" in response.content

    async def test_detail_page_shows_watches_section(self, client, db_session):

        db_session.add(Domain(name="watched.com"))
        await make_watch(
            db_session,
            name="My Watch",
            url="https://watched.com/page",
            content_type="html",
            effective_domain="watched.com",
        )
        response = await client.get("/domains/watched.com")
        assert b"Watches" in response.content
        assert b"My Watch" in response.content

    async def test_detail_page_shows_metadata(self, client, db_session):
        db_session.add(Domain(name="meta.com"))
        await db_session.flush()
        response = await client.get("/domains/meta.com")
        assert b"Metadata" in response.content

    async def test_detail_page_shows_danger_zone(self, client, db_session):
        db_session.add(Domain(name="danger.com"))
        await db_session.flush()
        response = await client.get("/domains/danger.com")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content


class TestDomainWatchesTableDomainInactiveBadge:
    async def test_suspended_watch_shows_domain_inactive_badge(self, client, db_session):
        db_session.add(Domain(name="ds-tbl.com", is_active=False))
        await make_watch(
            db_session,
            name="Suspended",
            url="https://ds-tbl.com/p",
            content_type="html",
            effective_domain="ds-tbl.com",
            is_active=False,
            domain_suspended=True,
        )
        response = await client.get("/domains/ds-tbl.com")
        assert b"Domain Inactive" in response.content

    async def test_manually_inactive_watch_does_not_show_domain_inactive(self, client, db_session):
        db_session.add(Domain(name="mi-tbl.com"))
        await make_watch(
            db_session,
            name="Manual Off",
            url="https://mi-tbl.com/p",
            content_type="html",
            effective_domain="mi-tbl.com",
            is_active=False,
            domain_suspended=False,
        )
        response = await client.get("/domains/mi-tbl.com")
        assert b"Domain Inactive" not in response.content


class TestDomainInlineUpdate:
    async def test_update_min_interval_htmx(self, client, db_session):
        db_session.add(Domain(name="update.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/update.com",
            data={"field": "min_interval", "value": "5.0"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200

    async def test_update_notes_htmx(self, client, db_session):
        db_session.add(Domain(name="notes-update.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/notes-update.com",
            data={"field": "notes", "value": "Updated note"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Updated note" in response.content

    async def test_update_invalid_field_returns_400(self, client, db_session):
        db_session.add(Domain(name="bad-field.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/bad-field.com",
            data={"field": "name", "value": "hacked"},
        )
        assert response.status_code == 400

    async def test_update_nonexistent_returns_404(self, client):
        response = await client.post(
            "/domains/nope.com",
            data={"field": "min_interval", "value": "5.0"},
        )
        assert response.status_code == 404

    async def test_update_non_htmx_redirects(self, client, db_session):
        db_session.add(Domain(name="redirect.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/redirect.com",
            data={"field": "min_interval", "value": "5.0"},
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestDomainArchive:
    async def test_archive_domain(self, client, db_session):
        db_session.add(Domain(name="to-archive.com"))
        await db_session.flush()
        response = await client.post("/domains/to-archive.com/archive", follow_redirects=False)
        assert response.status_code == 303

    async def test_archive_nonexistent_returns_404(self, client):
        response = await client.post("/domains/nope.com/archive")
        assert response.status_code == 404

    async def test_archive_button_has_htmx_body_target(self, client, db_session):
        """Archive button must carry hx-target=body so HTMX swaps the full page.

        Regression: missing hx-target caused the 303-redirect response to be
        injected as innerHTML of the button element (inside the Danger Zone).
        """
        db_session.add(Domain(name="htmx-arc-target.com"))
        await db_session.flush()
        response = await client.get("/domains/htmx-arc-target.com")
        assert response.status_code == 200
        content = response.content.decode()
        archive_section = content[content.find("Archive this domain") :]
        assert 'hx-target="body"' in archive_section


class TestDomainRestore:
    async def test_restore_domain(self, client, db_session):
        db_session.add(Domain(name="to-restore.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        response = await client.post("/domains/to-restore.com/restore", follow_redirects=False)
        assert response.status_code == 303

    async def test_restore_button_has_htmx_body_target(self, client, db_session):
        """Restore button must carry hx-target=body so HTMX swaps the full page.

        Regression: missing hx-target caused the 303-redirect response to be
        injected as innerHTML of the button element (inside the Danger Zone).
        """
        db_session.add(Domain(name="htmx-rst-target.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        response = await client.get("/domains/htmx-rst-target.com")
        assert response.status_code == 200
        content = response.content.decode()
        restore_section = content[content.find("Restore this domain") :]
        assert 'hx-target="body"' in restore_section


class TestDomainDelete:
    async def test_delete_archived_domain(self, client, db_session):
        """Successful delete returns 200 + HX-Redirect, not a bare 303.

        HX-Redirect allows HTMX to navigate the full page correctly; a plain 303
        would be followed by XHR and the page HTML would be swapped into the
        #danger-zone-error target element instead of replacing the page.
        """
        db_session.add(Domain(name="to-delete.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        response = await client.post("/domains/to-delete.com/delete", follow_redirects=False)
        assert response.status_code == 200
        assert response.headers.get("hx-redirect") == "/domains"

    async def test_delete_active_domain_returns_409(self, client, db_session):
        db_session.add(Domain(name="no-delete.com"))
        await db_session.flush()
        response = await client.post("/domains/no-delete.com/delete")
        assert response.status_code == 409

    async def test_delete_domain_with_watches_returns_409(self, client, db_session):

        db_session.add(Domain(name="busy-del.com", archived_at=datetime.now(UTC)))
        await make_watch(
            db_session,
            name="W",
            url="https://busy-del.com/p",
            content_type="html",
            effective_domain="busy-del.com",
        )
        response = await client.post("/domains/busy-del.com/delete")
        assert response.status_code == 409

    async def test_delete_nonexistent_returns_404(self, client):
        response = await client.post("/domains/nope.com/delete")
        assert response.status_code == 404


class TestDomainToggleActive:
    async def test_toggle_inactive_deactivates_domain(self, client, db_session):
        db_session.add(Domain(name="toggle-off.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/toggle-off.com/toggle-active",
            data={"active": "false"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        result = await db_session.execute(select(Domain).where(Domain.name == "toggle-off.com"))
        domain = result.scalar_one()
        assert domain.is_active is False

    async def test_toggle_active_reactivates_domain(self, client, db_session):
        db_session.add(Domain(name="toggle-on.com", is_active=False))
        await db_session.flush()
        response = await client.post(
            "/domains/toggle-on.com/toggle-active",
            data={"active": "true"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        result = await db_session.execute(select(Domain).where(Domain.name == "toggle-on.com"))
        domain = result.scalar_one()
        assert domain.is_active is True

    async def test_toggle_inactive_suspends_active_watches(self, client, db_session):
        db_session.add(Domain(name="suspend.com"))
        watch = await make_watch(
            db_session,
            name="Active Watch",
            url="https://suspend.com/p",
            content_type="html",
            effective_domain="suspend.com",
            is_active=True,
        )

        await client.post("/domains/suspend.com/toggle-active", data={"active": "false"})

        await db_session.refresh(watch)
        assert watch.is_active is False
        assert watch.domain_suspended is True

    async def test_toggle_inactive_skips_already_inactive_watches(self, client, db_session):
        db_session.add(Domain(name="skip-inactive.com"))
        watch = await make_watch(
            db_session,
            name="Already Inactive",
            url="https://skip-inactive.com/p",
            content_type="html",
            effective_domain="skip-inactive.com",
            is_active=False,
        )

        await client.post("/domains/skip-inactive.com/toggle-active", data={"active": "false"})

        await db_session.refresh(watch)
        assert watch.domain_suspended is False

    async def test_toggle_inactive_skips_archived_watches(self, client, db_session):
        db_session.add(Domain(name="skip-archived.com"))
        watch = await make_watch(
            db_session,
            name="Archived Watch",
            url="https://skip-archived.com/p",
            content_type="html",
            effective_domain="skip-archived.com",
            is_active=False,
            is_archived=True,
        )

        await client.post("/domains/skip-archived.com/toggle-active", data={"active": "false"})

        await db_session.refresh(watch)
        assert watch.domain_suspended is False

    async def test_toggle_active_restores_suspended_watches(self, client, db_session):
        db_session.add(Domain(name="restore.com", is_active=False))
        watch = await make_watch(
            db_session,
            name="Suspended Watch",
            url="https://restore.com/p",
            content_type="html",
            effective_domain="restore.com",
            is_active=False,
            domain_suspended=True,
        )

        await client.post("/domains/restore.com/toggle-active", data={"active": "true"})

        await db_session.refresh(watch)
        assert watch.is_active is True
        assert watch.domain_suspended is False

    async def test_toggle_active_does_not_restore_manually_inactive_watches(
        self, client, db_session
    ):
        db_session.add(Domain(name="manual.com", is_active=False))
        watch = await make_watch(
            db_session,
            name="Manual Inactive",
            url="https://manual.com/p",
            content_type="html",
            effective_domain="manual.com",
            is_active=False,
            domain_suspended=False,
        )

        await client.post("/domains/manual.com/toggle-active", data={"active": "true"})

        await db_session.refresh(watch)
        assert watch.is_active is False

    async def test_toggle_htmx_returns_partial(self, client, db_session):
        """Response includes both the toggle partial and an OOB watches table."""
        db_session.add(Domain(name="htmx-toggle.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/htmx-toggle.com/toggle-active",
            data={"active": "false"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"domain-status-toggle" in response.content
        assert b"domain-watches" in response.content
        assert b"hx-swap-oob" in response.content

    async def test_toggle_htmx_response_includes_watches_oob(self, client, db_session):
        """HTMX toggle response must include OOB swap for watches table."""
        db_session.add(Domain(name="htmx-oob.com"))
        await make_watch(
            db_session,
            name="OOB Watch",
            url="https://htmx-oob.com/p",
            content_type="html",
            effective_domain="htmx-oob.com",
            is_active=True,
        )
        await db_session.commit()
        response = await client.post(
            "/domains/htmx-oob.com/toggle-active",
            data={"active": "false"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"domain-watches" in response.content

    async def test_toggle_htmx_deactivate_shows_domain_inactive_in_watches(
        self, client, db_session
    ):
        """Watches table in OOB response shows Domain Inactive badge after deactivation."""
        db_session.add(Domain(name="htmx-badge.com"))
        await make_watch(
            db_session,
            name="Badge Watch",
            url="https://htmx-badge.com/p",
            content_type="html",
            effective_domain="htmx-badge.com",
            is_active=True,
        )
        await db_session.commit()
        response = await client.post(
            "/domains/htmx-badge.com/toggle-active",
            data={"active": "false"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Domain Inactive" in response.content

    async def test_toggle_nonexistent_returns_404(self, client):
        response = await client.post(
            "/domains/nope-toggle.com/toggle-active", data={"active": "false"}
        )
        assert response.status_code == 404

    async def test_toggle_archived_domain_returns_409(self, client, db_session):
        db_session.add(Domain(name="archived-toggle.com", archived_at=datetime.now(UTC)))
        await db_session.flush()
        response = await client.post(
            "/domains/archived-toggle.com/toggle-active", data={"active": "false"}
        )
        assert response.status_code == 409
