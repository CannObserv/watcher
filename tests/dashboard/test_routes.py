"""Integration tests for dashboard routes."""

import re

import pytest

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.watched_item import WatchedItem
from tests.conftest import make_info_item


async def _make_wi(db_session, *, name="Test WI", url="https://example.com"):
    """Create + flush a WatchedItem with effective_url; return it."""
    item = await make_info_item(db_session, name=name)
    wi = WatchedItem(
        archiver_info_item_id=item.info_item_id,
        name=name,
        effective_url=url,
    )
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


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
        assert b"Watched Items" in response.content


class TestPartialEndpoints:
    async def test_stats_cards_partial(self, client):
        response = await client.get("/partials/stats-cards")
        assert response.status_code == 200
        assert b"Total Watches" in response.content

    async def test_system_health_partial(self, client):
        response = await client.get("/partials/system-health")
        assert response.status_code == 200
        assert b"Task Queue" in response.content


class TestAuditLog:
    async def test_audit_page_returns_200(self, client):
        response = await client.get("/audit")
        assert response.status_code == 200
        assert b"Audit Log" in response.content

    async def test_audit_table_partial(self, client):
        response = await client.get("/partials/audit-table")
        assert response.status_code == 200

    async def test_audit_filter_by_event_type(self, client):
        response = await client.get("/partials/audit-table?event_type=watched_item.created")
        assert response.status_code == 200


class TestAuditTableSharedPartial:
    """The audit-table partial is shared by /audit and the WatchedItem detail
    page; scoping by watched_item_id hides the redundant column (#215)."""

    async def test_table_has_watched_item_column_by_default(self, client, db_session):
        db_session.add(AuditLog(event_type=EventType.WATCHED_ITEM_CREATED, payload={}))
        await db_session.commit()
        resp = await client.get("/partials/audit-table")
        assert b'<th scope="col">Watched Item</th>' in resp.content

    async def test_scoped_table_hides_watched_item_column(self, client, db_session):
        wi = await _make_wi(db_session, name="ScopedCol")
        resp = await client.get(f"/partials/audit-table?watched_item_id={wi.id}")
        assert resp.status_code == 200
        assert b'<th scope="col">Watched Item</th>' not in resp.content

    async def test_scoped_table_filters_to_item(self, client, db_session):
        wi = await _make_wi(db_session, name="OnlyMine")
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_NO_CHANGE,
                payload={"watched_item_id": str(wi.id)},
            )
        )
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_FETCH_FAILED,
                payload={"watched_item_id": "01OTHERITEMOTHERITEMOTHER"},
            )
        )
        await db_session.commit()
        resp = await client.get(f"/partials/audit-table?watched_item_id={wi.id}")
        assert EventType.CHECK_NO_CHANGE.encode() in resp.content
        assert EventType.CHECK_FETCH_FAILED.encode() not in resp.content

    async def test_pagination_renders_with_many_rows(self, client, db_session):
        for _ in range(26):  # > default page_size (25) -> 2 pages
            db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        await db_session.commit()
        resp = await client.get("/partials/audit-table")
        assert b'aria-label="Pagination"' in resp.content

    async def test_empty_state_copy(self, client):
        """Empty state is the simplified shared copy (#215 CR-2)."""
        resp = await client.get("/partials/audit-table")
        assert b"No entries found." in resp.content
        assert b"No audit entries found." not in resp.content

    async def test_invalid_page_size_does_not_error(self, client, db_session):
        """A negative page_size must not reach the DB as a negative LIMIT (#215 CR-3)."""
        db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        await db_session.commit()
        resp = await client.get("/partials/audit-table?page_size=-5")
        assert resp.status_code == 200

    async def test_out_of_range_page_size_is_capped(self, client, db_session):
        """An oversized page_size is capped at the max (100), not passed raw — the
        page-size <select> reflects the capped value (#215 CR-3/CR-6)."""
        for _ in range(30):
            db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        await db_session.commit()
        resp = await client.get("/partials/audit-table?page_size=99999")
        assert b'value="100" selected' in resp.content

    async def test_multiple_event_types_or_filter(self, client, db_session):
        """Multiple selected event-type chips OR together — not AND, not single-
        select replace (#215 bug). Distinct payload markers prove which rows show."""
        wi = await _make_wi(db_session, name="OrFilter")
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_SNAPSHOT_CREATED,
                payload={"watched_item_id": str(wi.id), "marker": "SNAProw"},
            )
        )
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_NO_CHANGE,
                payload={"watched_item_id": str(wi.id), "marker": "NOCHrow"},
            )
        )
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_FETCH_FAILED,
                payload={"watched_item_id": str(wi.id), "marker": "FAILrow"},
            )
        )
        await db_session.commit()
        resp = await client.get(
            f"/partials/audit-table?watched_item_id={wi.id}"
            f"&event_type={EventType.CHECK_SNAPSHOT_CREATED}"
            f"&event_type={EventType.CHECK_NO_CHANGE}"
        )
        body = resp.content
        assert b"SNAProw" in body  # both selected types are shown (OR)
        assert b"NOCHrow" in body
        assert b"FAILrow" not in body  # an unselected type stays excluded

    async def test_pager_links_preserve_all_selected_event_types(self, client, db_session):
        """Paging keeps every selected chip in the link — the qs() macro expands the
        list-valued event_type into repeated params (#215 bug)."""
        for _ in range(26):  # > page_size 25 -> a pager renders
            db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        await db_session.commit()
        body = (
            await client.get(
                f"/partials/audit-table?event_type={EventType.CHECK_NO_CHANGE}"
                f"&event_type={EventType.CHECK_FETCH_FAILED}"
            )
        ).content.decode()
        assert 'aria-label="Pagination"' in body
        assert "event_type=check.no_change" in body
        assert "event_type=check.fetch_failed" in body

    async def test_chips_submit_whole_checked_set(self, client):
        """Chips include the whole form on change, so every checked chip is sent —
        the wiring that gives OR and correct deselect behavior (#215 bug)."""
        resp = await client.get("/audit")
        assert b'hx-include="closest form"' in resp.content

    async def test_multiple_event_types_both_chips_checked(self, client):
        """A multi-value deep-link checks every selected chip (#215 bug)."""
        resp = await client.get(
            f"/audit?event_type={EventType.CHECK_NO_CHANGE}"
            f"&event_type={EventType.WATCHED_ITEM_CREATED}"
        )
        body = resp.content
        assert re.search(rb'value="check\.no_change"\s+checked', body)
        assert re.search(rb'value="watched_item\.created"\s+checked', body)

    async def test_page_size_options_derive_from_constant(self, client, db_session):
        """The page-size <select> renders exactly PAGE_SIZES — same constant that
        drives the clamp cap, so the two can't diverge (#215 CR-9)."""
        from src.dashboard.deps import PAGE_SIZES

        db_session.add(AuditLog(event_type=EventType.CHECK_NO_CHANGE, payload={}))
        await db_session.commit()
        # page_size=50 (!= 25) forces the pager to render even with one row.
        body = (await client.get("/partials/audit-table?page_size=50")).content.decode()
        for size in PAGE_SIZES:
            assert f'value="{size}"' in body
        assert body.count('<option value="') == len(PAGE_SIZES)


class Test404Template:
    async def test_watched_item_detail_404_uses_template(self, client):
        response = await client.get("/watched-items/not-a-ulid")
        assert response.status_code == 404
        assert b"Not Found" in response.content

    async def test_404_template_has_nav_link(self, client):
        response = await client.get("/watched-items/not-a-ulid")
        assert response.status_code == 404
        assert b"/watched-items" in response.content


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

    async def test_invalid_pagination_params_do_not_error(self, client):
        """Crafted negative page/page_size are clamped, not passed to the DB (#215 CR-6)."""
        assert (await client.get("/domains?page_size=-5")).status_code == 200
        assert (await client.get("/domains?page=-5")).status_code == 200
        assert (await client.get("/partials/domains-table?page_size=-5")).status_code == 200


class TestDomainDetailFilters:
    async def _create_domain_with_watch(self, client, db_session, item_name="Filter Watch"):
        """Create a domain and a WatchedItem whose domain_name matches it."""
        resp = await client.post(
            "/domains",
            data={"url": "https://example.com/page"},
            follow_redirects=False,
        )
        name = resp.headers["location"].rstrip("/").split("/")[-1]
        wi = await _make_wi(db_session, name=item_name, url=f"https://{name}/page")
        wi.domain_name = name
        await db_session.flush()
        await db_session.commit()
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

    async def test_audit_chips_use_current_event_types(self, client):
        """Stale legacy watch.* chips were corrected to watched_item.* (#215)."""
        body = (await client.get("/audit")).content
        assert b'value="watched_item.created"' in body
        assert b'value="watch.created"' not in body
        assert b'value="check.extraction_failed"' in body
