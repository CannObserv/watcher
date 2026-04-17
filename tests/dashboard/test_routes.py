"""Integration tests for dashboard routes."""

import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.notifications.events import WatchEventType

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

    async def test_recent_changes_partial(self, client):
        response = await client.get("/partials/recent-changes")
        assert response.status_code == 200

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
        from src.core.models.watch import WatchHealthStatus

        db_session.add(
            Watch(
                name="W",
                url="https://a.com",
                content_type="html",
                health_status=WatchHealthStatus.OK,
            )
        )
        await db_session.flush()
        response = await client.get("/watches")
        assert b"Healthy" in response.content

    async def test_health_badge_error(self, client, db_session):
        from src.core.models.watch import WatchHealthStatus

        db_session.add(
            Watch(
                name="W",
                url="https://a.com",
                content_type="html",
                health_status=WatchHealthStatus.ERROR,
            )
        )
        await db_session.flush()
        response = await client.get("/watches")
        assert b"Error" in response.content

    async def test_health_badge_unknown(self, client, db_session):
        db_session.add(Watch(name="W", url="https://a.com", content_type="html"))
        await db_session.flush()
        response = await client.get("/watches")
        assert b"Unknown" in response.content


class TestWatchDetail:
    async def test_detail_page_returns_200(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Detail Watch",
                "url": "https://example.com",
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


class TestWatchCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        assert b"New Watch" in response.content

    async def test_create_form_has_fields(self, client):
        response = await client.get("/watches/new")
        assert b'name="name"' in response.content
        assert b'name="url"' in response.content
        assert b'name="content_type"' in response.content

    async def test_create_watch_redirects(self, client):
        response = await client.post(
            "/watches/new",
            data={
                "name": "Created Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_missing_name_shows_error(self, client):
        response = await client.post(
            "/watches/new",
            data={
                "name": "",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()

    async def test_create_watch_sets_effective_domain_and_creates_domain_record(
        self, client, db_session
    ):
        response = await client.post(
            "/watches/new",
            data={
                "name": "Domain Test Watch",
                "url": "https://lcb.wa.gov/page",
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        watch_id = response.headers["location"].rstrip("/").split("/")[-1]
        result = await db_session.execute(select(Watch).where(Watch.id == watch_id))
        watch = result.scalar_one()
        assert watch.effective_url == "https://lcb.wa.gov/page"
        assert watch.effective_domain == "lcb.wa.gov"
        domain_result = await db_session.execute(select(Domain).where(Domain.name == "lcb.wa.gov"))
        assert domain_result.scalar_one_or_none() is not None

    async def test_create_watch_unreachable_url_shows_error(self, client):
        with patch(
            "src.dashboard.routes._create_watch",
            side_effect=httpx.ConnectError("unreachable"),
        ):
            response = await client.post(
                "/watches/new",
                data={
                    "name": "Bad Watch",
                    "url": "https://broken.example",
                    "content_type": "html",
                },
            )
        assert response.status_code == 200
        assert b"unreachable" in response.content.lower()


class TestWatchRowDomainInactiveBadge:
    async def test_domain_suspended_watch_shows_domain_inactive_badge(self, client, db_session):
        watch = Watch(
            name="Suspended Watch",
            url="https://ds-badge.com/p",
            content_type=ContentType.HTML,
            effective_domain="ds-badge.com",
            is_active=False,
            domain_suspended=True,
        )
        db_session.add(watch)
        await db_session.flush()
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200
        assert b"Domain Inactive" in response.content

    async def test_manually_inactive_watch_shows_inactive_not_domain_inactive(
        self, client, db_session
    ):
        watch = Watch(
            name="Manual Inactive",
            url="https://mi-badge.com/p",
            content_type=ContentType.HTML,
            effective_domain="mi-badge.com",
            is_active=False,
            domain_suspended=False,
        )
        db_session.add(watch)
        await db_session.flush()
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200
        assert b"Domain Inactive" not in response.content


class TestChangeDetail:
    async def test_change_detail_404_invalid(self, client):
        response = await client.get("/changes/bad-id")
        assert response.status_code == 404

    async def test_change_detail_404_not_found(self, client):
        response = await client.get("/changes/01JNZZZZZZZZZZZZZZZZZZZZZZ")
        assert response.status_code == 404

    async def test_change_detail_shows_screenshot_thumbnails(self, client, db_session):
        watch = Watch(name="Screenshotter", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap_defaults = dict(
            watch_id=watch.id,
            content_hash="a" * 64,
            simhash=0,
            storage_path="/tmp/s",
            text_path="/tmp/t",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        prev_snap = Snapshot(**snap_defaults, screenshot_path="screenshots/w/prev.png")
        curr_snap = Snapshot(**snap_defaults, screenshot_path="screenshots/w/curr.png")
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
        )
        db_session.add(change)
        await db_session.flush()

        response = await client.get(f"/changes/{change.id}")
        assert response.status_code == 200
        # Both snapshot_id params should appear in screenshot URLs
        assert str(prev_snap.id).encode() in response.content
        assert str(curr_snap.id).encode() in response.content
        assert b"screenshot" in response.content.lower()

    async def test_change_detail_no_screenshot_section_without_paths(self, client, db_session):
        watch = Watch(name="No Screenshot", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap_defaults = dict(
            watch_id=watch.id,
            content_hash="b" * 64,
            simhash=0,
            storage_path="/tmp/s",
            text_path="/tmp/t",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        prev_snap = Snapshot(**snap_defaults)
        curr_snap = Snapshot(**snap_defaults)
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
        )
        db_session.add(change)
        await db_session.flush()

        response = await client.get(f"/changes/{change.id}")
        assert response.status_code == 200
        # No screenshot section should appear — no snapshot_id= screenshot params
        assert b"Visual Comparison" not in response.content

    async def test_change_list_shows_visual_change_score_badge(self, client, db_session):
        watch = Watch(name="Visual Score Watch", url="https://example.com", content_type="html")
        db_session.add(watch)
        await db_session.flush()

        snap_defaults = dict(
            watch_id=watch.id,
            content_hash="c" * 64,
            simhash=0,
            storage_path="/tmp/s",
            text_path="/tmp/t",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        prev_snap = Snapshot(**snap_defaults)
        curr_snap = Snapshot(**snap_defaults)
        db_session.add_all([prev_snap, curr_snap])
        await db_session.flush()

        change = Change(
            watch_id=watch.id,
            previous_snapshot_id=prev_snap.id,
            current_snapshot_id=curr_snap.id,
            visual_change_score=0.85,
        )
        db_session.add(change)
        await db_session.flush()

        response = await client.get("/partials/recent-changes")
        assert response.status_code == 200
        assert b"85%" in response.content


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

    async def test_change_detail_404_uses_template(self, client):
        response = await client.get("/changes/01JNZZZZZZZZZZZZZZZZZZZZZZ")
        assert response.status_code == 404
        assert b"Not Found" in response.content

    async def test_404_template_has_nav_link(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404
        assert b"/watches" in response.content


_NOTIFY_PATCH = "src.dashboard.routes.dispatch_event_notifications"


class TestWatchArchive:
    async def _create_watch(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Archive Me", "url": "https://example.com/arc", "content_type": "html"},
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_archive_dispatches_watch_archived_event(self, client):
        watch_id = await self._create_watch(client)
        with patch(_NOTIFY_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.post(f"/watches/{watch_id}/archive")
        assert response.status_code in (200, 303)
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_ARCHIVED
        assert kwargs["event"].watch_id == watch_id

    async def test_archive_event_includes_name_and_url(self, client):
        watch_id = await self._create_watch(client)
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
        watch_id = await self._create_watch(client)
        with patch(_NOTIFY_PATCH, side_effect=Exception("notification failed")):
            response = await client.post(f"/watches/{watch_id}/archive")
        assert response.status_code in (200, 303)
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_archived is True

    async def test_archive_button_targets_body(self, client, db_session):
        """Archive button must carry hx-target=body so HTMX swaps the full page.

        Regression: missing hx-target caused the 303-redirect response to be
        injected as innerHTML of the button element (inside the Danger Zone
        section), resulting in the entire page rendering nested inside that div.
        """
        watch = Watch(name="Target Test", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()
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
        watch = Watch(
            name="Restore Target",
            url="https://example.com",
            content_type=ContentType.HTML,
            is_archived=True,
        )
        db_session.add(watch)
        await db_session.flush()
        response = await client.get(f"/watches/{watch.id}")
        assert response.status_code == 200
        content = response.content.decode()
        restore_section = content[content.find("Restore this watch") :]
        assert 'hx-target="body"' in restore_section


class TestWatchDeactivate:
    async def _create_active_watch(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Deactivate Me", "url": "https://example.com", "content_type": "html"},
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_deactivate_sets_watch_inactive(self, client, db_session):
        """POST /watches/{id}/deactivate must set is_active=False.

        Regression: the template called /deactivate but no route handler existed,
        so the button silently 404'd in production.
        """
        watch_id = await self._create_active_watch(client)
        response = await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        assert response.status_code == 303
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_active is False

    async def test_deactivate_htmx_returns_updated_row(self, client):
        watch_id = await self._create_active_watch(client)
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
        watch_id = await self._create_active_watch(client)
        # First deactivate
        await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        # Second deactivate — must not raise or error
        response = await client.post(f"/watches/{watch_id}/deactivate", follow_redirects=False)
        assert response.status_code == 303
        watch = await db_session.get(Watch, watch_id)
        await db_session.refresh(watch)
        assert watch.is_active is False

    async def test_deactivate_archived_watch_returns_409(self, client):
        """Deactivating an archived watch returns 409, consistent with toggle-active."""
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Arc Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        await client.post(f"/watches/{watch_id}/archive")
        response = await client.post(f"/watches/{watch_id}/deactivate")
        assert response.status_code == 409

    async def test_deactivate_already_inactive_htmx_returns_valid_row(self, client):
        """HTMX deactivate on an already-inactive watch must return the updated row.

        Idempotency: the route skips the commit when is_active is already False
        but must still render the row partial so HTMX can do the outerHTML swap.
        """
        watch_id = await self._create_active_watch(client)
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
    async def _create_and_archive(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Delete Me",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/watches/{watch_id}/archive")
        return watch_id

    async def test_delete_archived_watch_redirects(self, client):
        watch_id = await self._create_and_archive(client)
        response = await client.delete(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert response.headers.get("hx-redirect") == "/watches"

    async def test_delete_non_archived_watch_returns_409(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Cannot Delete Active",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.delete(f"/watches/{watch_id}")
        assert response.status_code == 409

    async def test_delete_missing_watch_returns_404(self, client):
        response = await client.delete("/watches/not-a-ulid")
        assert response.status_code == 404


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
    async def _create_domain_with_watch(self, client, watch_name="Filter Watch"):
        """Create a domain and a watch whose effective_domain matches it.

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
        await client.post(
            "/api/v1/watches",
            json={
                "name": watch_name,
                "url": f"https://{name}/page",
                "content_type": "html",
            },
        )
        return name

    async def test_domain_detail_has_segment_control(self, client):
        name = await self._create_domain_with_watch(client, "Domain Filter Watch")
        response = await client.get(f"/domains/{name}")
        body = response.content
        assert b'role="radiogroup"' in body
        assert b'name="status"' in body  # was: name="watch_status"

    async def test_domain_detail_no_filter_pill(self, client):
        name = await self._create_domain_with_watch(client, "Domain Filter Watch 2")
        response = await client.get(f"/domains/{name}")
        assert b"filter-pill" not in response.content

    async def test_domain_watches_partial(self, client):
        name = await self._create_domain_with_watch(client, "Partial Watch")
        response = await client.get(f"/partials/domain-watches/{name}")
        assert response.status_code == 200
        assert b"Partial Watch" in response.content

    async def test_domain_watches_partial_search(self, client):
        name = await self._create_domain_with_watch(client, "Searchable Watch")
        response = await client.get(f"/partials/domain-watches/{name}?q=searchable")
        assert response.status_code == 200
        assert b"Searchable Watch" in response.content

    async def test_domain_watches_partial_sort(self, client):
        name = await self._create_domain_with_watch(client, "Sort Watch")
        response = await client.get(f"/partials/domain-watches/{name}?sort=name&order=asc")
        assert response.status_code == 200


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
    async def _create_watch(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Timeline Watch", "url": "https://example.com", "content_type": "html"},
        )
        return resp.json()["id"]

    async def test_timeline_partial_returns_200(self, client):
        watch_id = await self._create_watch(client)
        response = await client.get(f"/partials/watch-timeline/{watch_id}")
        assert response.status_code == 200

    async def test_timeline_partial_404_invalid_id(self, client):
        response = await client.get("/partials/watch-timeline/not-a-ulid")
        assert response.status_code == 404

    async def test_detail_page_shows_timeline_section(self, client):
        watch_id = await self._create_watch(client)
        response = await client.get(f"/watches/{watch_id}")
        assert response.status_code == 200
        # Should show "Event Timeline" heading, not old "Change History"
        assert b"Event Timeline" in response.content

    async def test_detail_page_no_change_history_heading(self, client):
        watch_id = await self._create_watch(client)
        response = await client.get(f"/watches/{watch_id}")
        # Old section heading should be gone
        assert b"Change History" not in response.content

    async def test_timeline_partial_filter_param(self, client):
        watch_id = await self._create_watch(client)
        for category in ("all", "changes", "errors", "config"):
            response = await client.get(f"/partials/watch-timeline/{watch_id}?category={category}")
            assert response.status_code == 200


class TestScreenshotRecapture:
    async def _create_watch(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Recapture Watch", "url": "https://example.com", "content_type": "html"},
        )
        return resp.json()["id"]

    async def test_recapture_no_snapshot_returns_404(self, client):
        watch_id = await self._create_watch(client)
        response = await client.post(f"/watches/{watch_id}/screenshot")
        assert response.status_code == 404

    async def test_recapture_missing_watch_returns_404(self, client):
        response = await client.post("/watches/not-a-ulid/screenshot")
        assert response.status_code == 404

    async def test_recapture_playwright_unavailable_returns_200_unavailable(
        self, client, db_session, monkeypatch
    ):
        watch = Watch(name="No PW Watch", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        snap = Snapshot(
            watch_id=watch.id,
            content_hash="a" * 64,
            simhash=0,
            storage_path="snapshots/w/s.html",
            text_path="snapshots/w/s.txt",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snap)
        await db_session.flush()

        import src.dashboard.routes as routes_mod

        monkeypatch.setattr(routes_mod, "capture_screenshot", lambda *a, **kw: _async_none())
        response = await client.post(f"/watches/{watch.id}/screenshot")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unavailable"

    async def test_recapture_success_returns_200_ok(
        self, client, db_session, monkeypatch, tmp_path
    ):
        watch = Watch(name="PW Watch", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

        snap = Snapshot(
            watch_id=watch.id,
            content_hash="b" * 64,
            simhash=0,
            storage_path="snapshots/w/s.html",
            text_path="snapshots/w/s.txt",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snap)
        await db_session.flush()

        import src.dashboard.routes as routes_mod
        from src.core.screenshot import ScreenshotResult

        fake_png = b"\x89PNG\r\n"
        fake_result = ScreenshotResult(png_bytes=fake_png, browser="Chromium 0")
        monkeypatch.setattr(
            routes_mod,
            "capture_screenshot",
            lambda *a, **kw: _async_result(fake_result),
        )
        monkeypatch.setattr(routes_mod, "STORAGE_BASE_DIR", tmp_path)

        response = await client.post(f"/watches/{watch.id}/screenshot")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "screenshot_path" in data


async def _async_none():
    return None


async def _async_result(value):
    return value


class TestSnapshotContentViewer:
    async def _create_watch(self, client):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Content Watch", "url": "https://example.com", "content_type": "html"},
        )
        return resp.json()["id"]

    async def test_content_missing_watch_returns_404(self, client):
        url = "/watches/not-a-ulid/snapshots/01JNZZZZZZZZZZZZZZZZZZZZZZ/content"
        response = await client.get(url)
        assert response.status_code == 404

    async def test_content_missing_snapshot_returns_404(self, client):
        watch_id = await self._create_watch(client)
        response = await client.get(
            f"/watches/{watch_id}/snapshots/01JNZZZZZZZZZZZZZZZZZZZZZZ/content"
        )
        assert response.status_code == 404

    async def test_content_no_storage_path_returns_404(self, client, db_session):
        watch = Watch(
            name="No Path Watch", url="https://example.com", content_type=ContentType.HTML
        )
        db_session.add(watch)
        await db_session.flush()

        snap = Snapshot(
            watch_id=watch.id,
            content_hash="c" * 64,
            simhash=0,
            storage_path="snapshots/w/nonexistent.html",
            text_path="snapshots/w/nonexistent.txt",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snap)
        await db_session.flush()

        response = await client.get(f"/watches/{watch.id}/snapshots/{snap.id}/content")
        assert response.status_code == 404

    async def test_content_serves_text_path(self, client, db_session, monkeypatch, tmp_path):
        watch = Watch(
            name="Text Path Watch", url="https://example.com", content_type=ContentType.HTML
        )
        db_session.add(watch)
        await db_session.flush()

        text_path = "snapshots/w/snap.txt"
        full_path = tmp_path / text_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"Hello extracted content")

        snap = Snapshot(
            watch_id=watch.id,
            content_hash="d" * 64,
            simhash=0,
            storage_path="snapshots/w/snap.html",
            text_path=text_path,
            chunk_count=1,
            text_bytes=23,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snap)
        await db_session.flush()

        import src.dashboard.routes as routes_mod

        monkeypatch.setattr(routes_mod, "STORAGE_BASE_DIR", tmp_path)

        response = await client.get(f"/watches/{watch.id}/snapshots/{snap.id}/content")
        assert response.status_code == 200
        assert b"Hello extracted content" in response.content

    async def test_content_falls_back_to_storage_path(
        self, client, db_session, monkeypatch, tmp_path
    ):
        watch = Watch(
            name="Fallback Watch", url="https://example.com", content_type=ContentType.HTML
        )
        db_session.add(watch)
        await db_session.flush()

        storage_path = "snapshots/w/snap_raw.html"
        full_path = tmp_path / storage_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"<html>raw content</html>")

        snap = Snapshot(
            watch_id=watch.id,
            content_hash="e" * 64,
            simhash=0,
            storage_path=storage_path,
            text_path="snapshots/w/nonexistent.txt",
            chunk_count=1,
            text_bytes=23,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snap)
        await db_session.flush()

        import src.dashboard.routes as routes_mod

        monkeypatch.setattr(routes_mod, "STORAGE_BASE_DIR", tmp_path)

        response = await client.get(f"/watches/{watch.id}/snapshots/{snap.id}/content")
        assert response.status_code == 200
        assert b"raw content" in response.content
