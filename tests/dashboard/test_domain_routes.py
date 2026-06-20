"""Integration tests for domain dashboard routes."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.domain import Domain
from tests.conftest import make_watched_item

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

    async def test_active_filter_excludes_inactive(self, client, db_session):
        """The Active filter must not surface deactivated (is_active=False) domains (#190)."""
        db_session.add(Domain(name="act-on.com", is_active=True))
        db_session.add(Domain(name="act-off.com", is_active=False))
        await db_session.flush()
        response = await client.get("/partials/domains-table?status=active")
        assert response.status_code == 200
        assert b"act-on.com" in response.content
        assert b"act-off.com" not in response.content

    async def test_inactive_filter_includes_only_inactive(self, client, db_session):
        db_session.add(Domain(name="inact-on.com", is_active=True))
        db_session.add(Domain(name="inact-off.com", is_active=False))
        await db_session.flush()
        response = await client.get("/partials/domains-table?status=inactive")
        assert response.status_code == 200
        assert b"inact-off.com" in response.content
        assert b"inact-on.com" not in response.content

    async def test_inactive_filter_segment_present(self, client):
        response = await client.get("/domains")
        assert b'value="inactive"' in response.content

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

    async def test_detail_page_shows_watched_items_section(self, client, db_session):

        db_session.add(Domain(name="watched.com"))
        await make_watched_item(
            db_session,
            name="My Watch",
            primary_url="https://watched.com/page",
            default_content_type="html",
            domain_name="watched.com",
        )
        response = await client.get("/domains/watched.com")
        assert b"Watched Items" in response.content
        assert b"My Watch" in response.content

    async def test_detail_page_shows_metadata(self, client, db_session):
        db_session.add(Domain(name="meta.com"))
        await db_session.flush()
        response = await client.get("/domains/meta.com")
        assert b"Metadata" in response.content


class TestDomainDefaultScheduleConfigDashboard:
    """#205: dashboard cadence edit on the domain detail page + back-fill."""

    async def test_detail_page_shows_cadence_field(self, client, db_session):
        db_session.add(Domain(name="cadence-ui.com", default_schedule_config={"interval": "7d"}))
        await db_session.flush()
        response = await client.get("/domains/cadence-ui.com")
        assert b"Check Cadence" in response.content
        assert b"7d" in response.content

    async def test_post_sets_cadence_and_redirects(self, client, db_session):
        db_session.add(Domain(name="set-cadence.com"))
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            "/domains/set-cadence.com/default-schedule-config",
            data={"interval": "6h"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "set-cadence.com"))
        ).scalar_one()
        await db_session.refresh(domain)
        assert domain.default_schedule_config == {"interval": "6h"}

    async def test_post_backfills_items(self, client, db_session):
        db_session.add(Domain(name="ui-backfill.com"))
        wi = await make_watched_item(
            db_session,
            name="OnUiDomain",
            primary_url="https://ui-backfill.com/p",
            domain_name="ui-backfill.com",
        )
        await db_session.commit()
        await client.post(
            "/domains/ui-backfill.com/default-schedule-config",
            data={"interval": "6h"},
            follow_redirects=False,
        )
        from src.core.models.watched_item import WatchedItem

        refreshed = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi.id))
        ).scalar_one()
        await db_session.refresh(refreshed)
        assert refreshed.domain_default_schedule_config == {"interval": "6h"}

    async def test_post_blank_clears_cadence(self, client, db_session):
        db_session.add(Domain(name="clear-ui.com", default_schedule_config={"interval": "6h"}))
        await db_session.flush()
        await db_session.commit()
        await client.post(
            "/domains/clear-ui.com/default-schedule-config",
            data={"interval": ""},
            follow_redirects=False,
        )
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "clear-ui.com"))
        ).scalar_one()
        await db_session.refresh(domain)
        assert domain.default_schedule_config is None

    async def test_post_bad_interval_rerenders_with_flash(self, client, db_session):
        """#205 CR: a malformed interval re-renders the detail page with an error flash (400),
        not a bare error response."""
        db_session.add(Domain(name="bad-ui.com"))
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            "/domains/bad-ui.com/default-schedule-config",
            data={"interval": "soon"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        body = response.content.decode()
        assert "Invalid cadence" in body  # friendly flash, not a raw error page
        assert "Check Cadence" in body  # the detail page was re-rendered

    async def test_post_unchanged_on_bad_interval(self, client, db_session):
        """A rejected cadence leaves the domain's stored value untouched."""
        db_session.add(Domain(name="keep-ui.com", default_schedule_config={"interval": "6h"}))
        await db_session.flush()
        await db_session.commit()
        await client.post(
            "/domains/keep-ui.com/default-schedule-config",
            data={"interval": "soon"},
            follow_redirects=False,
        )
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "keep-ui.com"))
        ).scalar_one()
        await db_session.refresh(domain)
        assert domain.default_schedule_config == {"interval": "6h"}

    async def test_detail_page_shows_danger_zone(self, client, db_session):
        db_session.add(Domain(name="danger.com"))
        await db_session.flush()
        response = await client.get("/domains/danger.com")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content


class TestDomainWatchedItemsTableDomainInactiveBadge:
    async def test_suspended_watched_item_shows_domain_inactive_badge(self, client, db_session):
        db_session.add(Domain(name="ds-tbl.com", is_active=False))
        await make_watched_item(
            db_session,
            name="Suspended",
            primary_url="https://ds-tbl.com/p",
            default_content_type="html",
            domain_name="ds-tbl.com",
            is_active=False,
            domain_suspended=True,
        )
        response = await client.get("/domains/ds-tbl.com")
        assert b"Domain Inactive" in response.content

    async def test_manually_inactive_watched_item_does_not_show_domain_inactive(
        self, client, db_session
    ):
        db_session.add(Domain(name="mi-tbl.com"))
        await make_watched_item(
            db_session,
            name="Manual Off",
            primary_url="https://mi-tbl.com/p",
            default_content_type="html",
            domain_name="mi-tbl.com",
            is_active=False,
            domain_suspended=False,
        )
        response = await client.get("/domains/mi-tbl.com")
        assert b"Domain Inactive" not in response.content

    async def test_table_has_health_and_interval_columns(self, client, db_session):
        """Domain WatchedItems table surfaces Health + Interval columns (#190)."""
        db_session.add(Domain(name="cols.com"))
        await make_watched_item(
            db_session,
            name="ColsWatch",
            primary_url="https://cols.com/p",
            default_content_type="html",
            domain_name="cols.com",
        )
        response = await client.get("/partials/domain-watched-items/cols.com")
        assert response.status_code == 200
        assert b"Health" in response.content
        assert b"Interval" in response.content

    async def test_table_health_badge_resolves_string_status(self, client, db_session):
        """#202 CR: health_status loads as a plain str — the badge must reflect it.

        The prior `wi.health_status.value` access yielded Jinja Undefined and the
        column always rendered the 'Unknown' fallback, regardless of real health.
        """
        db_session.add(Domain(name="health-tbl.com"))
        await make_watched_item(
            db_session,
            name="HealthyWatch",
            primary_url="https://health-tbl.com/p",
            domain_name="health-tbl.com",
            health_status="ok",
        )
        response = await client.get("/partials/domain-watched-items/health-tbl.com")
        assert response.status_code == 200
        assert b"Healthy" in response.content
        assert b"badge-active" in response.content


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

    async def test_delete_domain_with_watched_items_returns_409(self, client, db_session):

        db_session.add(Domain(name="busy-del.com", archived_at=datetime.now(UTC)))
        await make_watched_item(
            db_session,
            name="W",
            primary_url="https://busy-del.com/p",
            default_content_type="html",
            domain_name="busy-del.com",
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

    async def test_toggle_inactive_suspends_watched_items(self, client, db_session):
        """#191: domain deactivation sets ``domain_suspended`` on the WatchedItem."""
        db_session.add(Domain(name="suspend.com"))
        wi = await make_watched_item(
            db_session,
            name="Active Item",
            primary_url="https://suspend.com/p",
            domain_name="suspend.com",
            is_active=True,
        )

        await client.post("/domains/suspend.com/toggle-active", data={"active": "false"})

        await db_session.refresh(wi)
        assert wi.domain_suspended is True

    async def test_toggle_inactive_suspends_regardless_of_active_state(self, client, db_session):
        """domain_suspended is set on every WatchedItem on the domain, active or not."""
        db_session.add(Domain(name="skip-inactive.com"))
        wi = await make_watched_item(
            db_session,
            name="Already Inactive",
            primary_url="https://skip-inactive.com/p",
            domain_name="skip-inactive.com",
            is_active=False,
        )

        await client.post("/domains/skip-inactive.com/toggle-active", data={"active": "false"})

        await db_session.refresh(wi)
        assert wi.domain_suspended is True

    async def test_toggle_inactive_suspends_archived_items_too(self, client, db_session):
        from datetime import UTC, datetime

        db_session.add(Domain(name="skip-archived.com"))
        wi = await make_watched_item(
            db_session,
            name="Archived Item",
            primary_url="https://skip-archived.com/p",
            domain_name="skip-archived.com",
            is_active=False,
            archived_at=datetime.now(UTC),
        )

        await client.post("/domains/skip-archived.com/toggle-active", data={"active": "false"})

        await db_session.refresh(wi)
        assert wi.domain_suspended is True

    async def test_toggle_active_clears_suspension(self, client, db_session):
        db_session.add(Domain(name="restore.com", is_active=False))
        wi = await make_watched_item(
            db_session,
            name="Suspended Item",
            primary_url="https://restore.com/p",
            domain_name="restore.com",
            is_active=False,
            domain_suspended=True,
        )

        await client.post("/domains/restore.com/toggle-active", data={"active": "true"})

        await db_session.refresh(wi)
        assert wi.domain_suspended is False

    async def test_toggle_active_does_not_restore_manually_inactive_watched_items(
        self, client, db_session
    ):
        db_session.add(Domain(name="manual.com", is_active=False))
        wi = await make_watched_item(
            db_session,
            name="Manual Inactive",
            primary_url="https://manual.com/p",
            default_content_type="html",
            domain_name="manual.com",
            is_active=False,
            domain_suspended=False,
        )

        await client.post("/domains/manual.com/toggle-active", data={"active": "true"})

        await db_session.refresh(wi)
        assert wi.is_active is False

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

    async def test_toggle_htmx_response_includes_watched_items_oob(self, client, db_session):
        """HTMX toggle response must include OOB swap for the watched-items table."""
        db_session.add(Domain(name="htmx-oob.com"))
        await make_watched_item(
            db_session,
            name="OOB Watch",
            primary_url="https://htmx-oob.com/p",
            default_content_type="html",
            domain_name="htmx-oob.com",
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

    async def test_toggle_htmx_deactivate_shows_domain_inactive_in_watched_items(
        self, client, db_session
    ):
        """Watched-items table in OOB response shows Domain Inactive badge after deactivation."""
        db_session.add(Domain(name="htmx-badge.com"))
        await make_watched_item(
            db_session,
            name="Badge Watch",
            primary_url="https://htmx-badge.com/p",
            default_content_type="html",
            domain_name="htmx-badge.com",
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
