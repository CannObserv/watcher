"""Integration tests for dashboard routes."""

import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from archiver_client import NotFound
from sqlalchemy import select

from src.core.models.domain import Domain
from src.core.models.watch import ContentType, Watch, WatchHealthStatus
from src.core.notifications.events import WatchEventType
from tests.conftest import (
    bind_primary_source,
    bind_sub_aspect,
    make_info_item,
    make_info_source,
    make_watch,
)


async def _seed_info_item(db_session, *, name="Test InfoItem", url="https://example.com"):
    """Create + commit an InfoItem with a bound primary InfoSource; return info_item_id (str).

    #160: dashboard form takes ``info_item_id`` only (with optional
    sub_aspect ULID). The primary URL is resolved server-side via the
    ArchiverClient — tests need the binding present so the SDK mock can find it.
    """
    item = await make_info_item(db_session, name=name)
    primary = await make_info_source(db_session, url=url)
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=primary.info_source_id,
    )
    await db_session.commit()
    return str(item.info_item_id)


pytestmark = pytest.mark.integration


class TestDashboardHome:
    async def test_home_returns_200(self, client):
        response = await client.get("/")
        assert response.status_code == 200

    async def test_home_contains_title(self, client):
        response = await client.get("/")
        assert b"watcher" in response.content.lower()

    async def test_home_contains_nav(self, client):
        response = await client.get("/")
        assert b"Dashboard" in response.content
        assert b"Watches" in response.content


class TestPartialEndpoints:
    async def test_stats_cards_partial(self, client):
        response = await client.get("/partials/stats-cards")
        assert response.status_code == 200
        assert b"Total Watches" in response.content

    async def test_system_health_partial(self, client):
        response = await client.get("/partials/system-health")
        assert response.status_code == 200
        assert b"Task Queue" in response.content


class TestWatchList:
    async def test_watches_page_returns_200(self, client):
        response = await client.get("/watches")
        assert response.status_code == 200
        assert b"Watches" in response.content

    async def test_watches_page_has_create_link(self, client):
        response = await client.get("/watches")
        assert b"/watches/new" in response.content

    async def test_watch_table_partial(self, client):
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200

    async def test_watch_table_filter(self, client):
        response = await client.get("/partials/watch-table?status=active")  # was: ?is_active=true
        assert response.status_code == 200

    async def test_health_badge_ok(self, client, db_session):
        await make_watch(
            db_session,
            name="W",
            primary_url="https://a.com",
            content_type="html",
            health_status=WatchHealthStatus.OK,
        )
        response = await client.get("/watches")
        assert b"Healthy" in response.content

    async def test_health_badge_error(self, client, db_session):
        await make_watch(
            db_session,
            name="W",
            primary_url="https://a.com",
            content_type="html",
            health_status=WatchHealthStatus.ERROR,
        )
        response = await client.get("/watches")
        assert b"Error" in response.content

    async def test_health_badge_unknown(self, client, db_session):
        await make_watch(db_session, name="W", primary_url="https://a.com", content_type="html")
        response = await client.get("/watches")
        assert b"Unknown" in response.content


class TestWatchDetail:
    async def test_detail_page_returns_200(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Detail Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Detail Watch",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert b"Detail Watch" in response.content

    async def test_detail_page_404_invalid(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404

    async def test_detail_page_shows_resolved_url_no_inline_edit(self, client, db_session):
        """Detail page renders ``resolved_url`` plainly — no Edit handle on it."""
        watch = await make_watch(
            db_session, name="No URL Edit", primary_url="https://example.com/x", content_type="html"
        )
        await db_session.commit()
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        # The URL row no longer renders the Edit-mode field partial.
        assert b"https://example.com/x" in response.content
        # No GET endpoint for ``url`` field anymore.
        assert b"/field/url" not in response.content


class TestWatchCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        assert b"New Watch" in response.content

    async def test_create_form_renders_typeahead_picker(self, client):
        response = await client.get("/watches/new")
        body = response.content
        # The form switched from ULID-paste to typeahead.
        assert b'role="combobox"' in body
        assert b'hx-get="/info-items/search' in body
        # Picker binding-tree container is present (info_item_id injected dynamically by picker).
        assert b'id="watch-create-binding-tree"' in body
        # Power-user paste-ULID fallback is wrapped in <details>.
        assert b"<details" in body
        assert b"Paste ULID" in body
        # Legacy minimal-picker hint text is gone (was rendered always by target_picker.html).
        assert b"Paste the InfoItem ULID from the Archiver service" not in body

    async def test_create_form_prepopulates_with_valid_watched_item_id(self, client, db_session):
        """watched_item_id param triggers hx-trigger=load on the binding-tree div."""
        from src.core.models.watched_item import WatchedItem

        info_item_id = await _seed_info_item(db_session, name="Pre-pop Item")
        wi = WatchedItem(info_item_id=info_item_id, name="Pre-pop WI")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watches/new?watched_item_id={wi.id}")
        assert response.status_code == 200
        body = response.content
        assert b'hx-trigger="load"' in body
        assert (b"/info-items/" + info_item_id.encode()) in body

    async def test_create_form_degrades_for_unknown_watched_item_id(self, client):
        """Unknown watched_item_id returns 200 with no pre-population load trigger."""
        from ulid import ULID

        response = await client.get(f"/watches/new?watched_item_id={ULID()}")
        assert response.status_code == 200
        assert b'hx-trigger="load"' not in response.content

    async def test_create_watch_redirects(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Created Watch")
        response = await client.post(
            "/watches/new",
            data={
                "name": "Created Watch",
                "info_item_id": info_item_id,
                "watch-create__target": "",  # primary
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_with_subaspect_target(self, client, db_session):
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        sub = await make_info_source(db_session, parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=sub.info_source_id,
        )
        await db_session.commit()
        response = await client.post(
            "/watches/new",
            data={
                "name": "Sub Watch",
                "info_item_id": str(item.info_item_id),
                "watch-create__target": str(sub.info_source_id),
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_via_paste_ulid_fallback(self, client, db_session):
        """When picker is empty, fall back to manual paste fields."""
        info_item_id = await _seed_info_item(db_session, name="Manual Paste")
        response = await client.post(
            "/watches/new",
            data={
                "name": "Manual Paste",
                "info_item_id": "",  # picker empty
                "info_item_id_manual": info_item_id,
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_missing_name_shows_error(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="X")
        response = await client.post(
            "/watches/new",
            data={
                "name": "",
                "info_item_id": info_item_id,
                "watch-create__target": "",
                "content_type": "html",
            },
        )
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()

    async def test_create_watch_sets_domain_name_and_creates_domain_record(
        self, client, db_session
    ):
        info_item_id = await _seed_info_item(
            db_session, name="Domain Test Watch", url="https://lcb.wa.gov/page"
        )
        response = await client.post(
            "/watches/new",
            data={
                "name": "Domain Test Watch",
                "info_item_id": info_item_id,
                "watch-create__target": "",
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        watch_id = response.headers["location"].rstrip("/").split("/")[-1]
        result = await db_session.execute(select(Watch).where(Watch.id == watch_id))
        watch = result.scalar_one()
        await db_session.refresh(watch, ["watched_item"])
        assert watch.effective_url == "https://lcb.wa.gov/page"
        assert watch.watched_item.domain_name == "lcb.wa.gov"
        domain_result = await db_session.execute(select(Domain).where(Domain.name == "lcb.wa.gov"))
        assert domain_result.scalar_one_or_none() is not None

    async def test_create_watch_unreachable_url_shows_error(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Bad Watch")
        with patch(
            "src.dashboard.routes._create_watch",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("unreachable"),
        ):
            response = await client.post(
                "/watches/new",
                data={
                    "name": "Bad Watch",
                    "info_item_id": info_item_id,
                    "watch-create__target": "",
                    "content_type": "html",
                },
            )
        assert response.status_code == 200
        assert b"unreachable" in response.content.lower()

    async def test_create_watch_info_item_only_redirects(self, client, db_session):
        """Minimal POST — info_item_id via picker, no target, no extras."""
        info_item_id = await _seed_info_item(db_session, name="Minimal Watch")
        response = await client.post(
            "/watches/new",
            data={
                "name": "Minimal Watch",
                "info_item_id": info_item_id,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/watches/")

    async def test_create_watch_bad_info_item_id_shows_flash(self, client, db_session):
        """Unknown info_item_id → 200 with flash, not a 5xx."""
        await _seed_info_item(db_session, name="Decoy")
        bogus_id = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        with patch(
            "src.dashboard.routes._create_watch",
            new_callable=AsyncMock,
            side_effect=NotFound(f"info_item {bogus_id} not found"),
        ):
            response = await client.post(
                "/watches/new",
                data={
                    "name": "Bogus Target",
                    "info_item_id": bogus_id,
                    "watch-create__target": "",
                    "content_type": "html",
                },
            )
        assert response.status_code == 200
        body = response.content.lower()
        assert b"does not exist" in body
        assert bogus_id.encode().lower() in body
        assert b"New Watch" in response.content


class TestWatchListNoScheduleColumn:
    """#160 Task 11.1: the list table no longer surfaces a schedule_config column."""

    async def test_list_page_omits_schedule_config_column(self, client, db_session):
        await make_watch(
            db_session,
            name="Listed Watch",
            primary_url="https://list-cols.example.com",
            content_type="html",
        )
        response = await client.get("/watches")
        assert response.status_code == 200
        # Legacy schedule_config header / cell is gone; raw column-name reference
        # must not leak into the rendered table.
        assert b"schedule_config" not in response.content

    async def test_table_partial_omits_schedule_config_column(self, client, db_session):
        await make_watch(
            db_session,
            name="Partial Watch",
            primary_url="https://partial-cols.example.com",
            content_type="html",
        )
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200
        assert b"schedule_config" not in response.content


class TestWatchInlineEditUrlGone:
    """The inline-edit endpoint for the dropped ``url`` field must not exist."""

    async def test_get_url_field_inline_edit_returns_4xx(self, client, db_session):
        watch = await make_watch(
            db_session, name="No URL Edit", primary_url="https://example.com", content_type="html"
        )
        await db_session.commit()
        response = await client.get(
            f"/watches/{watch.id}/field/url",
            headers={"HX-Request": "true"},
        )
        # Either 400 (dispatcher rejects) or 404 (route gone). Anything but 200.
        assert response.status_code in (400, 404, 405)

    async def test_post_url_field_inline_edit_returns_4xx(self, client, db_session):
        watch = await make_watch(
            db_session, name="No URL Edit", primary_url="https://example.com", content_type="html"
        )
        await db_session.commit()
        response = await client.post(
            f"/watches/{watch.id}/field/url",
            data={"value": "https://other.example"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code in (400, 404, 405)

    async def test_post_timeout_field_inline_edit_returns_4xx(self, client, db_session):
        """fetch_config keys are gone too — ``timeout`` was the canonical example."""
        watch = await make_watch(
            db_session,
            name="No timeout edit",
            primary_url="https://example.com",
            content_type="html",
        )
        await db_session.commit()
        response = await client.post(
            f"/watches/{watch.id}/field/timeout",
            data={"value": "60"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code in (400, 404, 405)


class TestWatchRowDomainInactiveBadge:
    async def test_domain_suspended_watch_shows_domain_inactive_badge(self, client, db_session):
        await make_watch(
            db_session,
            name="Suspended Watch",
            primary_url="https://ds-badge.com/p",
            content_type=ContentType.HTML,
            domain_name="ds-badge.com",
            is_active=False,
            domain_suspended=True,
        )
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200
        assert b"Domain Inactive" in response.content

    async def test_manually_inactive_watch_shows_inactive_not_domain_inactive(
        self, client, db_session
    ):
        await make_watch(
            db_session,
            name="Manual Inactive",
            primary_url="https://mi-badge.com/p",
            content_type=ContentType.HTML,
            domain_name="mi-badge.com",
            is_active=False,
            domain_suspended=False,
        )
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200
        assert b"Domain Inactive" not in response.content


class TestAuditLog:
    async def test_audit_page_returns_200(self, client):
        response = await client.get("/audit")
        assert response.status_code == 200
        assert b"Audit Log" in response.content

    async def test_audit_table_partial(self, client):
        response = await client.get("/partials/audit-table")
        assert response.status_code == 200

    async def test_audit_filter_by_event_type(self, client):
        response = await client.get("/partials/audit-table?event_type=watch.created")
        assert response.status_code == 200


class Test404Template:
    async def test_watch_detail_404_uses_template(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404
        assert b"Not Found" in response.content

    async def test_404_template_has_nav_link(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404
        assert b"/watches" in response.content


_NOTIFY_PATCH = "src.dashboard.routes.dispatch_event_notifications"


class TestWatchArchive:
    async def _create_watch(self, client, db_session):
        info_item_id = await _seed_info_item(
            db_session, name="Archive Me", url="https://example.com/arc"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Archive Me",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_archive_dispatches_watch_archived_event(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        with patch(_NOTIFY_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.post(f"/watches/{watch_id}/archive")
        assert response.status_code in (200, 303)
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_ARCHIVED
        assert kwargs["event"].watch_id == watch_id

    async def test_archive_event_includes_name_and_url(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        with patch(_NOTIFY_PATCH, new_callable=AsyncMock) as mock_dispatch:
            await client.post(f"/watches/{watch_id}/archive")
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.watch_name == "Archive Me"
        assert event.watch_url == "https://example.com/arc"

    async def test_archive_commits_even_if_notification_raises(self, client, db_session):
        """Commit must not be gated on notification success.

        Regression: dispatch was called before session.commit(), so a network
        failure in Apprise would prevent the archive from ever being persisted.
        """
        watch_id = await self._create_watch(client, db_session)
        with patch(_NOTIFY_PATCH, side_effect=Exception("notification failed")):
            response = await client.post(f"/watches/{watch_id}/archive")
        assert response.status_code in (200, 303)
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_archived is True

    async def test_archive_completes_when_resolve_watch_url_raises(self, client, db_session):
        """SDK failure resolving the URL must NOT roll back the archive.

        Regression: ``resolve_watch_url`` was called AFTER ``session.commit()``
        but its exception escaped as a 500, leaking a partial-success state to
        the operator (archive persisted but UI reports failure). The handler
        must log + dispatch with a sentinel URL (or skip dispatch) and still
        return the redirect.
        """
        watch = await make_watch(
            db_session,
            name="Resolve Fails",
            primary_url="https://example.com/resolve-fails",
            content_type=ContentType.HTML,
        )
        await db_session.commit()
        with patch(
            "src.dashboard.routes.resolve_watch_url",
            new_callable=AsyncMock,
            side_effect=RuntimeError("information service unreachable"),
        ):
            response = await client.post(f"/watches/{watch.id}/archive")
        assert response.status_code in (200, 303), (
            f"archive must complete despite SDK failure; got {response.status_code}"
        )
        # archive must be persisted regardless
        db_watch = await db_session.get(Watch, watch.id)
        await db_session.refresh(db_watch)
        assert db_watch.is_archived is True
        assert db_watch.is_active is False

    async def test_archive_button_targets_body(self, client, db_session):
        """Archive button must carry hx-target=body so HTMX swaps the full page.

        Regression: missing hx-target caused the 303-redirect response to be
        injected as innerHTML of the button element (inside the Danger Zone
        section), resulting in the entire page rendering nested inside that div.
        """
        watch = await make_watch(
            db_session,
            name="Target Test",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        content = response.content.decode()
        # The archive form/button must specify hx-target="body"
        archive_section = content[content.find("Archive this watch") :]
        assert 'hx-target="body"' in archive_section

    async def test_restore_button_targets_body(self, client, db_session):
        """Restore button must carry hx-target=body so HTMX swaps the full page.

        Regression: missing hx-target would cause the 303-redirect response to
        be injected as innerHTML of the button element (inside the Danger Zone).
        """
        watch = await make_watch(
            db_session,
            name="Restore Target",
            primary_url="https://example.com",
            content_type=ContentType.HTML,
            is_archived=True,
        )
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        content = response.content.decode()
        restore_section = content[content.find("Restore this watch") :]
        assert 'hx-target="body"' in restore_section


class TestWatchDeactivate:
    async def _create_active_watch(self, client, db_session):
        info_item_id = await _seed_info_item(
            db_session, name="Deactivate Me", url="https://example.com"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Deactivate Me",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_deactivate_sets_watch_inactive(self, client, db_session):
        """POST /watches/{id}/deactivate must set is_active=False.

        Regression: the template called /deactivate but no route handler existed,
        so the button silently 404'd in production.
        """
        watch_id = await self._create_active_watch(client, db_session)
        response = await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        assert response.status_code == 303
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_active is False

    async def test_deactivate_htmx_returns_updated_row(self, client, db_session):
        watch_id = await self._create_active_watch(client, db_session)
        response = await client.post(
            f"/watches/{watch_id}/deactivate",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert f'id="watch-{watch_id}"' in content
        assert "Inactive" in content

    async def test_deactivate_already_inactive_is_idempotent(self, client, db_session):
        """Deactivating an already-inactive watch returns 303 without error."""
        watch_id = await self._create_active_watch(client, db_session)
        # First deactivate
        await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        # Second deactivate — must not raise or error
        response = await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        assert response.status_code == 303
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_active is False

    async def test_deactivate_archived_watch_returns_409(self, client, db_session):
        """Deactivating an archived watch returns 409, consistent with toggle-active."""
        info_item_id = await _seed_info_item(db_session, name="Arc Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Arc Watch",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/watches/{watch_id}/archive")
        response = await client.post(f"/watches/{watch_id}/deactivate")
        assert response.status_code == 409

    async def test_deactivate_already_inactive_htmx_returns_valid_row(self, client, db_session):
        """HTMX deactivate on an already-inactive watch must return the updated row.

        Idempotency: the route skips the commit when is_active is already False
        but must still render the row partial so HTMX can do the outerHTML swap.
        """
        watch_id = await self._create_active_watch(client, db_session)
        # First deactivate (non-HTMX) to make the watch inactive
        await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        # Second deactivate via HTMX — must render the row, not 303-redirect
        response = await client.post(
            f"/watches/{watch_id}/deactivate",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert f'id="watch-{watch_id}"' in content
        assert "Inactive" in content

    async def test_deactivate_missing_watch_returns_404(self, client):
        response = await client.post("/watches/not-a-ulid/deactivate")
        assert response.status_code == 404


class TestWatchDelete:
    async def _create_and_archive(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Delete Me")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Delete Me",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/watches/{watch_id}/archive")
        return watch_id

    async def test_delete_archived_watch_redirects(self, client, db_session):
        watch_id = await self._create_and_archive(client, db_session)
        response = await client.delete(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert response.headers.get("hx-redirect") == "/watches"

    async def test_delete_non_archived_watch_returns_409(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Cannot Delete Active")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Cannot Delete Active",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.delete(f"/watches/{watch_id}")
        assert response.status_code == 409

    async def test_delete_missing_watch_returns_404(self, client):
        response = await client.delete("/watches/not-a-ulid")
        assert response.status_code == 404

    async def test_delete_primary_with_active_sub_aspect_sibling_renders_sibling_message(
        self, client, db_session
    ):
        """Dashboard surfaces the sub_aspect-sibling reason, not the generic archive prompt."""
        from sqlalchemy import select
        from ulid import ULID

        from tests._information_test_models import InfoItemSource
        from tests.conftest import bind_sub_aspect, make_info_source

        info_item_id = await _seed_info_item(db_session, name="Has Sibling")
        # Look up the primary InfoSource that _seed_info_item bound.
        primary_iss = (
            await db_session.execute(
                select(InfoItemSource)
                .where(InfoItemSource.info_item_id == ULID.from_str(info_item_id))
                .where(InfoItemSource.role.is_(None))
            )
        ).scalar_one()
        primary_info_source_id = primary_iss.info_source_id

        primary_resp = await client.post(
            "/api/v1/watches",
            json={"name": "Primary", "info_item_id": info_item_id, "content_type": "html"},
        )
        primary_id = primary_resp.json()["id"]

        # Create a sub_aspect fragment under the primary InfoSource and bind it.
        fragment = await make_info_source(
            db_session,
            parent_info_source_id=primary_info_source_id,
        )
        await bind_sub_aspect(
            db_session,
            info_item_id=ULID.from_str(info_item_id),
            info_source_id=fragment.info_source_id,
        )
        await db_session.commit()

        await client.post(
            "/api/v1/watches",
            json={
                "name": "Sub",
                "info_item_id": info_item_id,
                "target_info_source_id": str(fragment.info_source_id),
                "content_type": "html",
            },
        )

        await client.post(f"/watches/{primary_id}/archive")
        response = await client.delete(f"/watches/{primary_id}")
        assert response.status_code == 409
        assert b"sub_aspect" in response.content
        assert b"Archive the watch before deleting it" not in response.content


class TestDomainsPage:
    async def test_domains_page_returns_200(self, client):
        response = await client.get("/domains")
        assert response.status_code == 200
        assert b"Domains" in response.content

    async def test_domains_page_has_segment_control(self, client):
        response = await client.get("/domains")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="status"' in body
        assert b'type="radio"' in body

    async def test_domains_page_active_filter_checked(self, client):
        """Default status is 'active', so the active radio should be checked."""
        response = await client.get("/domains")
        body = response.text
        # The "active" radio should have the checked attribute
        assert re.search(r'value="active"\s+checked', body)

    async def test_domains_page_no_filter_pill(self, client):
        """filter-pill class should not appear in domains page."""
        response = await client.get("/domains")
        assert b"filter-pill" not in response.content


class TestWatchListFilters:
    async def test_watches_page_has_segment_control(self, client):
        response = await client.get("/watches")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="status"' in body
        assert b'type="radio"' in body

    async def test_watch_table_filter_by_status(self, client):
        response = await client.get("/partials/watch-table?status=active")
        assert response.status_code == 200

    async def test_watch_table_search(self, client):
        response = await client.get("/partials/watch-table?q=something")
        assert response.status_code == 200

    async def test_watch_table_domain_filter(self, client):
        response = await client.get("/partials/watch-table?domain=example.com")
        assert response.status_code == 200

    async def test_watch_table_sort(self, client):
        response = await client.get("/partials/watch-table?sort=name&order=asc")
        assert response.status_code == 200


class TestDomainDetailFilters:
    async def _create_domain_with_watch(self, client, db_session, watch_name="Filter Watch"):
        """Create a domain and a watch whose domain_name matches it.

        The mock probe extracts hostname from URL, so the watch URL must
        use the domain name as its hostname for the watch to appear in
        the domain's watch list.
        """
        resp = await client.post(
            "/domains",
            data={"url": "https://example.com/page"},
            follow_redirects=False,
        )
        name = resp.headers["location"].rstrip("/").split("/")[-1]
        info_item_id = await _seed_info_item(
            db_session, name=watch_name, url=f"https://{name}/page"
        )
        await client.post(
            "/api/v1/watches",
            json={
                "name": watch_name,
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        return name

    async def test_domain_detail_no_filter_pill(self, client, db_session):
        name = await self._create_domain_with_watch(client, db_session, "Domain Filter Watch 2")
        response = await client.get(f"/domains/{name}")
        assert b"filter-pill" not in response.content

    async def test_domain_watched_items_partial(self, client, db_session):
        name = await self._create_domain_with_watch(client, db_session, "Partial Watch")
        response = await client.get(f"/partials/domain-watched-items/{name}")
        assert response.status_code == 200
        assert b"Partial Watch" in response.content

    async def test_domain_watched_items_partial_search(self, client, db_session):
        name = await self._create_domain_with_watch(client, db_session, "Searchable Watch")
        response = await client.get(f"/partials/domain-watched-items/{name}?q=searchable")
        assert response.status_code == 200
        assert b"Searchable Watch" in response.content

    async def test_domain_watched_items_partial_sort_and_status(self, client, db_session):
        name = await self._create_domain_with_watch(client, db_session, "Sort Status Watch")
        response = await client.get(
            f"/partials/domain-watched-items/{name}?sort=last_checked_at&order=desc&status=active"
        )
        assert response.status_code == 200
        assert b"Sort Status Watch" in response.content

    async def test_domain_watched_items_partial_status_archived_empty(self, client, db_session):
        name = await self._create_domain_with_watch(client, db_session, "Archived Filter Watch")
        response = await client.get(f"/partials/domain-watched-items/{name}?status=archived")
        assert response.status_code == 200
        assert b"No watched items" in response.content


class TestAuditLogFilters:
    async def test_audit_page_has_chip_group(self, client):
        response = await client.get("/audit")
        body = response.content
        assert b'class="chip-group"' in body
        assert b'type="checkbox"' in body
        assert b'name="event_type"' in body

    async def test_audit_page_no_filter_pill(self, client):
        response = await client.get("/audit")
        assert b"filter-pill" not in response.content


class TestWatchTimeline:
    async def _create_watch(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Timeline Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Timeline Watch",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        return resp.json()["id"]

    async def test_timeline_partial_returns_200(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        response = await client.get(f"/partials/watch-timeline/{watch_id}")
        assert response.status_code == 200

    async def test_timeline_partial_404_invalid_id(self, client):
        response = await client.get("/partials/watch-timeline/not-a-ulid")
        assert response.status_code == 404

    async def test_detail_page_shows_timeline_section(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        response = await client.get(f"/watches/{watch_id}")
        assert response.status_code == 200
        # Should show "Event Timeline" heading, not old "Change History"
        assert b"Event Timeline" in response.content

    async def test_detail_page_no_change_history_heading(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        response = await client.get(f"/watches/{watch_id}")
        # Old section heading should be gone
        assert b"Change History" not in response.content

    async def test_timeline_partial_filter_param(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        for category in ("all", "changes", "errors", "config"):
            response = await client.get(f"/partials/watch-timeline/{watch_id}?category={category}")
            assert response.status_code == 200


# Phase 5 (#156): TestScreenshotRecapture + TestSnapshotContentViewer removed.
# Screenshot recapture and snapshot content viewer routes dropped with Snapshot table.
