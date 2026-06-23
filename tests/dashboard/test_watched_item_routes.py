"""Integration tests for WatchedItem dashboard routes (#185 Phase A step 7)."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_template import (
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watched_item import WatchedItem
from src.dashboard.routes import (
    _apply_watched_item_field_update,
    _build_schedule_map,
    _watched_item_field_context,
)
from tests.conftest import make_info_item, make_watched_item

pytestmark = pytest.mark.integration


class TestListPage:
    async def test_returns_200(self, client):
        response = await client.get("/watched-items")
        assert response.status_code == 200

    async def test_empty_state_renders_cta(self, client):
        response = await client.get("/watched-items")
        body = response.content
        assert b"No watched items yet" in body
        # CTA is the URL-first WatchedItem create; the stale /watches/new button
        # (which errored without a watched_item_id) was removed in #190.
        assert b"/watched-items/new" in body
        assert b"/watches/new" not in body

    async def test_list_renders_items(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name="Listed"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"Listed" in response.content

    async def test_sidebar_link_present(self, client):
        response = await client.get("/")
        assert b'href="/watched-items"' in response.content

    async def test_removed_columns_absent(self, client, db_session):
        """Information Item, Content Type, Tags, Last Reviewed columns are gone."""

        item = await make_info_item(db_session)
        db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name="ColTest"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        body = response.content
        assert b"Information Item" not in body
        assert b"Content Type" not in body
        assert b"Last Reviewed" not in body
        assert b"Tags" not in body

    async def test_new_column_headers_present(self, client, db_session):
        """Last Check, Next Check, Status headers appear; Aspect Review removed."""

        item = await make_info_item(db_session)
        db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name="ColTest2"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        body = response.content
        assert b"Last Check" in body
        assert b"Next Check" in body
        assert b"Aspect Review" not in body

    async def test_next_check_has_data_attribute_when_last_checked(self, client, db_session):
        """Rows with last_checked_at render a data-next-check ISO timestamp."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="WithCheck",
            last_checked_at=datetime.now(UTC),
            default_schedule_config={"interval": "1h"},
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"data-next-check" in response.content

    async def test_interval_column_shows_inherited_system_default(self, client, db_session):
        """#204: an item with no schedule config shows the inherited system default
        (1d) in the Interval column, not a blank em dash."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="InheritInterval",
            default_schedule_config=None,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        body = response.content.decode()
        # Cell-level: the resolved value sits in the Interval cell span followed by
        # the "· default" inherited marker — not a bare "1d" matching incidentally.
        assert ">1d <span" in body
        assert "· default" in body  # SYSTEM_DEFAULT_SCHEDULE_CONFIG, flagged inherited

    async def test_next_check_computed_for_inherited_schedule(self, client, db_session):
        """#204: a checked item with no schedule config still renders a Next Check —
        the interval resolves to the system default, so next check is computable."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="InheritNextCheck",
            last_checked_at=datetime.now(UTC),
            default_schedule_config=None,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"data-next-check" in response.content

    async def test_explicit_interval_has_no_default_marker(self, client, db_session):
        """#204: an explicit interval renders the value with no '· default' marker —
        the inherited marker is reserved for items that fall back to a default."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="ExplicitInterval",
            default_schedule_config={"interval": "6h"},
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        body = response.content.decode()
        assert ">6h<" in body  # value rendered in the Interval cell span
        assert "· default" not in body  # no inherited marker for an explicit interval

    async def test_domain_inherited_interval_shows_domain_marker(self, client, db_session):
        """#205: an item inheriting its domain's cadence shows '7d · domain', not '· default'."""
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="InheritDomain",
            default_schedule_config=None,
            domain_default_schedule_config={"interval": "7d"},
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        body = (await client.get("/watched-items")).content.decode()
        assert ">7d <span" in body
        assert "· domain" in body
        assert "· default" not in body

    async def test_active_profile_shows_profile_cadence(self, client, db_session):
        """#206 (the #204 CR finding-2 fix): a list item whose temporal profile is
        currently active shows the profile cadence + '· profile', matching
        schedule_tick — not the base 1d the UI used to display."""
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="ProfileRamp",
            default_schedule_config={"interval": "1d"},
            last_checked_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db_session.add(wi)
        await db_session.flush()
        db_session.add(
            TemporalProfile(
                watched_item_id=wi.id,
                profile_type=ProfileType.EVENT,
                reference_date=date.today() + timedelta(days=10),
                rules=[{"days_before": 3650, "interval": "1h"}],
                post_action=PostAction.DEACTIVATE,
            )
        )
        await db_session.commit()
        body = (await client.get("/watched-items")).content.decode()
        assert ">1h <span" in body  # profile cadence in the Interval cell
        assert "· profile" in body

    async def test_status_column_consolidated(self, client, db_session):
        """One labeled Status column holds the toggle + badge; no separate Actions column (#190)."""

        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item.info_item_id,
                name="ConsolidatedRow",
                effective_url="https://example.com",
                is_active=True,
            )
        )
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        body = response.content
        # Toggle lives in the Status column now…
        assert b"/toggle-active" in body
        assert b"Check now" in body
        # …and the separate unlabeled Actions header is gone.
        assert b'sr-only">Actions' not in body

    async def test_aspect_review_column_removed(self, client, db_session):
        """Aspect Review column removed from list view (#173)."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="HtmxRow")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"aspect-review-status" not in response.content


class TestDetailPage:
    async def test_returns_200(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Detail Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Detail Test" in response.content

    async def test_404_unknown(self, client):
        response = await client.get(f"/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_shows_effective_url(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="URL Test",
            effective_url="https://example.org/foo",
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"https://example.org/foo" in response.content

    async def test_no_binding_tree(self, client, db_session):
        """Binding tree removed in step 7 — no info_item_picker partials."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="No Tree WI")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        assert b"binding_tree" not in body
        assert b"info_item_picker" not in body
        # No radio inputs (those were part of the tree)
        assert b'type="radio"' not in body

    async def test_renders_danger_zone(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Danger")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content

    async def test_notification_templates_panel_renders_item_template(self, client, db_session):
        """#200: the item-template panel renders an item-scoped template's title + badge.

        Post-#200 item notifications are NotificationTemplate rows with
        visibility='watched_item'; the detail page surfaces them in the
        "Notification Templates" panel (the separate "Notification Configs" panel
        is gone — the per-Watch tier folded in #191, the model unified in #200).
        """

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="WithConfig")
        db_session.add(wi)
        await db_session.flush()
        db_session.add(
            NotificationTemplate(
                visibility=VISIBILITY_WATCHED_ITEM,
                watched_item_id=wi.id,
                title="Ops Slack",
                channel_hint="slack",
                events=["change_detected"],
                is_active=True,
            )
        )
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        assert b"Notification Templates" in body
        # Label uses title and the active badge renders.
        assert b"Ops Slack" in body
        assert b"badge-active" in body

    async def test_temporal_profile_panel_renders(self, client, db_session):
        """#191 CR-5: the WatchedItem detail surfaces its 1:1 temporal profile."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="WithProfile")
        db_session.add(wi)
        await db_session.flush()
        db_session.add(
            TemporalProfile(
                watched_item_id=wi.id,
                profile_type=ProfileType.EVENT,
                reference_date=date(2026, 7, 1),
                rules=[{"days_before": 7, "interval": "1h"}],
                post_action=PostAction.REDUCE_FREQUENCY,
            )
        )
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        assert b"Temporal Profile" in body
        assert b"2026-07-01" in body

    async def test_temporal_profile_panel_empty_state(self, client, db_session):
        """No profile → the panel shows the default-interval hint."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="NoProfile")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"No temporal profile" in response.content

    async def test_domain_suspended_banner_renders(self, client, db_session):
        """Domain Inactive alert shows when watched_item.domain_suspended=True."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id, name="Suspended Item", domain_suspended=True
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Domain Inactive" in response.content

    async def test_domain_name_link_renders(self, client, db_session):
        """Domain link appears and points to /domains/<name> when domain_name is set."""

        db_session.add(Domain(name="detail-domain.com"))
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="Domain Link Item",
            domain_name="detail-domain.com",
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"/domains/detail-domain.com" in response.content
        assert b"detail-domain.com" in response.content

    async def test_new_watch_button_hidden_when_archived(self, client, db_session):
        """New Watch button absent on archived WI."""

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="Archived",
            effective_url="https://example.com/target",
            archived_at=datetime.now(UTC),
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"+ New Watch" not in response.content

    async def test_new_watch_button_hidden_when_no_effective_url(self, client, db_session):
        """New Watch button absent when WatchedItem has no effective_url."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="NoPrimary")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"+ New Watch" not in response.content

    async def test_detail_page_renders_200(self, client, db_session):
        """Regression: health_map must be passed when rendering the detail page."""
        wi = await make_watched_item(db_session, name="Detail Item")
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Detail Item" in response.content

    async def test_detail_heading_has_watched_item_eyebrow(self, client, db_session):
        """#202: page heading shows a subdued 'Watched Item' kicker above the name."""
        wi = await _make_wi(db_session, name="EyebrowItem")
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b">Watched Item</p>" in body

    async def test_detail_health_inline_with_last_checked(self, client, db_session):
        """#202: Health badge moves inline after the Last Checked value (no standalone row).

        Uses the string health_status the DB actually loads (StrEnum-backed String
        column) — the prior `.value` access yielded Undefined and always rendered
        'unknown' in production.
        """
        wi = await _make_wi(db_session, name="HealthInline", health_status="ok")
        body = (await client.get(f"/watched-items/{wi.id}")).content.decode()
        # No standalone Health row label anymore…
        assert ">Health</span>" not in body
        # …badge names itself + describes the detail via aria-describedby → sr-only span.
        tip_id = f"wi-health-tip-{wi.id}"
        assert 'aria-label="Health: ok"' in body
        assert f'aria-describedby="{tip_id}"' in body
        assert f'id="{tip_id}"' in body
        assert "The last check completed successfully." in body
        assert body.index("Last Checked") < body.index('aria-label="Health: ok"')

    async def test_detail_row_order(self, client, db_session):
        """#202: Details rows render Name, URL, Domain, Status, Last Checked,
        Last Changed, Interval, Content Type, Description, Tags — in that order."""
        db_session.add(Domain(name="order-domain.com"))
        wi = await _make_wi(
            db_session,
            name="OrderItem",
            effective_url="https://example.com",
            domain_name="order-domain.com",
        )
        body = (await client.get(f"/watched-items/{wi.id}")).content.decode()
        labels = [
            ">Name</span>",
            ">URL</span>",
            ">Domain</span>",
            ">Status</span>",
            ">Last Checked</span>",
            ">Last Changed</span>",
            ">Interval</span>",
            ">Content Type</span>",
            ">Description</span>",
            ">Tags</span>",
        ]
        positions = [body.index(s) for s in labels]
        assert positions == sorted(positions), positions

    async def test_detail_consolidates_into_details_panel(self, client, db_session):
        """#202: the top panels merge into one 'Details' panel; 'Defaults' is gone."""
        wi = await _make_wi(db_session, name="PanelMerge", effective_url="https://example.com")
        resp = await client.get(f"/watched-items/{wi.id}")
        body = resp.content
        assert b">Details</h3>" in body
        assert b">Defaults</h3>" not in body

    async def test_detail_field_labels_drop_default_prefix(self, client, db_session):
        """#202: rows read Interval / Content Type / Tags, not 'Default …'."""
        wi = await _make_wi(db_session, name="LabelRename", effective_url="https://example.com")
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b">Interval</" in body
        assert b">Content Type</" in body
        assert b">Tags</" in body
        assert b"Default Interval" not in body
        assert b"Default Content Type" not in body
        assert b"Default Tags" not in body

    async def test_detail_interval_shows_inherited_system_default(self, client, db_session):
        """#202: an item with no schedule config shows the inherited system default."""
        wi = await _make_wi(db_session, name="InheritInterval")  # default_schedule_config=None
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"1d" in body  # SYSTEM_DEFAULT_SCHEDULE_CONFIG interval
        assert "· default".encode() in body

    async def test_detail_interval_empty_config_shows_braces_not_marker(self, client, db_session):
        """#202 CR: an explicit empty schedule config shows '{ }', not a blank '· default'."""
        wi = await _make_wi(db_session, name="EmptyConfig", default_schedule_config={})
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"{ }" in body
        assert "· default".encode() not in body

    async def test_detail_interval_shows_active_profile_cadence(self, client, db_session):
        """#206 (the #204 CR finding-2 fix): an item whose temporal profile is
        currently active shows the profile cadence + '· profile' on the detail
        interval row, matching what schedule_tick actually does — not the base 1d."""
        wi = await _make_wi(
            db_session, name="ProfileDetail", default_schedule_config={"interval": "1d"}
        )
        db_session.add(
            TemporalProfile(
                watched_item_id=wi.id,
                profile_type=ProfileType.EVENT,
                reference_date=date.today() + timedelta(days=10),
                rules=[{"days_before": 3650, "interval": "1h"}],
                post_action=PostAction.DEACTIVATE,
            )
        )
        await db_session.commit()
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert "· profile".encode() in body

    async def test_detail_interval_shows_domain_marker(self, client, db_session):
        """#205: detail shows '7d · domain' for an item inheriting its domain cadence."""
        wi = await _make_wi(
            db_session,
            name="InheritDomainDetail",
            default_schedule_config=None,
            domain_default_schedule_config={"interval": "7d"},
        )
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"7d" in body
        assert "· domain".encode() in body
        assert "· default".encode() not in body

    async def test_detail_url_row_has_edit_affordance(self, client, db_session):
        """#202: URL row exposes an inline Edit button (no 'Change URL' details)."""
        wi = await _make_wi(db_session, name="UrlEdit", effective_url="https://example.com")
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"Change URL" not in body
        assert b"/effective-url/field?mode=edit" in body

    async def test_url_field_partial_edit_mode(self, client, db_session):
        """#202: the URL field GET route serves an edit form posting to /effective-url."""
        wi = await _make_wi(db_session, name="UrlField", effective_url="https://example.com")
        resp = await client.get(
            f"/watched-items/{wi.id}/effective-url/field?mode=edit",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b'name="url"' in resp.content
        assert f"/watched-items/{wi.id}/effective-url".encode() in resp.content

    async def test_detail_activity_after_notification_templates(self, client, db_session):
        """#202: Recent Activity renders below the Notification Templates panel."""
        wi = await _make_wi(db_session, name="OrderCheck")
        body = (await client.get(f"/watched-items/{wi.id}")).content.decode()
        assert body.index("Notification Templates") < body.index("Recent Activity")

    async def test_detail_table_uses_static_headers_not_global_partial(self, client, db_session):
        """Regression #182: sort buttons must NOT point at /partials/watch-table."""
        wi = await make_watched_item(db_session, name="Scoped Item")
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Scoped Item" in response.content
        assert b'hx-get="/partials/watch-table"' not in response.content

    async def test_aspect_review_status_route_gone(self, client, db_session):
        """The /aspect-review-status route was removed in step 7."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Review Gone")
        db_session.add(wi)
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/aspect-review-status",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 404


class TestListPageSearchAndPagination:
    async def test_partial_route_returns_200(self, client):
        response = await client.get("/partials/watched-items-table")
        assert response.status_code == 200

    async def test_search_filters_by_name(self, client, db_session):
        item_a = await make_info_item(db_session, name="Alpha Item")
        item_b = await make_info_item(db_session, name="Beta Item")
        db_session.add(WatchedItem(archiver_info_item_id=item_a.info_item_id, name="Alpha WI"))
        db_session.add(WatchedItem(archiver_info_item_id=item_b.info_item_id, name="Beta WI"))
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?q=Alpha")
        body = response.content
        assert b"Alpha WI" in body
        assert b"Beta WI" not in body

    async def test_search_is_case_insensitive(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(archiver_info_item_id=item.info_item_id, name="Cannabis Observer")
        )
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?q=cannabis")
        assert b"Cannabis Observer" in response.content

    async def test_pagination_returns_page_two(self, client, db_session):
        for name in ("AAA", "BBB", "CCC"):
            item = await make_info_item(db_session, name=name)
            db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name=name))
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?page=2&page_size=2")
        body = response.content
        assert b"CCC" in body
        assert b"AAA" not in body
        assert b"BBB" not in body

    async def test_include_archived_false_hides_archived(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item.info_item_id,
                name="Archived WI",
                archived_at=datetime.now(UTC),
            )
        )
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table")
        assert b"Archived WI" not in response.content

    async def test_include_archived_true_shows_archived(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item.info_item_id,
                name="ShowArchived WI",
                archived_at=datetime.now(UTC),
            )
        )
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?include_archived=true")
        assert b"ShowArchived WI" in response.content

    async def test_include_archived_false_explicit_param(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(
                archiver_info_item_id=item.info_item_id,
                name="HiddenArchived",
                archived_at=datetime.now(UTC),
            )
        )
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?include_archived=false")
        assert response.status_code == 200
        assert b"HiddenArchived" not in response.content

    async def test_full_page_hx_target_and_include_in_pagination_context(self, client, db_session):
        for name in ("PA", "PB", "PC"):
            item = await make_info_item(db_session, name=name)
            db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name=name))
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/watched-items?page_size=2")
        body = response.content
        assert b"watched-items-table-container" in body
        assert b"domains-table-container" not in body

    async def test_empty_state_on_no_search_matches(self, client):
        response = await client.get("/partials/watched-items-table?q=xyzzy_no_match")
        assert response.status_code == 200
        assert b"No watched items" in response.content

    async def test_full_page_renders_search_bar(self, client):
        response = await client.get("/watched-items")
        body = response.content
        assert b'name="q"' in body
        assert b"Filter by name" in body

    async def test_no_aspect_review_column(self, client, db_session):
        item = await make_info_item(db_session)
        db_session.add(WatchedItem(archiver_info_item_id=item.info_item_id, name="NoAR"))
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/watched-items")
        body = response.content
        assert b"Aspect Review" not in body
        assert b"aspect-review-status" not in body


class TestArchiveRestore:
    async def test_archive_redirects_back(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="ToArchive")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)
        assert response.status_code in (200, 303)

    async def test_archive_marks_watched_item(self, client, db_session):
        """#191: archiving the single-entity WatchedItem stamps archived_at + inactive."""

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Parent")
        db_session.add(wi)
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)

        await db_session.refresh(wi)
        assert wi.archived_at is not None
        assert wi.is_active is False

    async def test_restore_clears_archived_at(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="Arc",
            archived_at=datetime.now(UTC),
            is_active=False,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/restore", follow_redirects=False)
        await db_session.refresh(wi)
        assert wi.archived_at is None


class TestPermanentDelete:
    """Danger-Zone permanent delete — archived-only affordance (#210)."""

    async def test_archived_detail_shows_delete_block(self, client, db_session):
        wi = await make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        await db_session.commit()
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"Delete permanently" in body
        assert f"/watched-items/{wi.id}/delete".encode() in body

    async def test_active_detail_hides_delete_block(self, client, db_session):
        wi = await make_watched_item(db_session, name="Active")
        await db_session.commit()
        body = (await client.get(f"/watched-items/{wi.id}")).content
        assert b"Delete permanently" not in body
        assert f"/watched-items/{wi.id}/delete".encode() not in body

    async def test_delete_htmx_redirects_to_list(self, client, db_session):
        wi = await make_watched_item(
            db_session, name="Gone", archived_at=datetime.now(UTC), is_active=False
        )
        await db_session.commit()
        wi_id = wi.id

        response = await client.post(
            f"/watched-items/{wi_id}/delete",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/watched-items"

        gone = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert gone is None

    async def test_delete_non_htmx_redirects_to_list(self, client, db_session):
        wi = await make_watched_item(
            db_session, name="Gone2", archived_at=datetime.now(UTC), is_active=False
        )
        await db_session.commit()
        wi_id = wi.id

        response = await client.post(f"/watched-items/{wi_id}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/watched-items"

        gone = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert gone is None

    async def test_delete_non_archived_keeps_row_and_flashes(self, client, db_session):
        wi = await make_watched_item(db_session, name="StillActive")
        await db_session.commit()
        wi_id = wi.id

        response = await client.post(
            f"/watched-items/{wi_id}/delete",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert response.status_code == 200  # OOB flash, not a redirect

        still = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert still is not None

    async def test_delete_unknown_returns_404(self, client):
        response = await client.post(f"/watched-items/{ULID()}/delete", follow_redirects=False)
        assert response.status_code == 404


class TestFieldHelpers:
    def test_interval_format(self):
        wi = MagicMock()
        wi.default_schedule_config = {"interval": "15m"}
        wi.domain_default_schedule_config = None
        wi.last_checked_at = None
        ctx = _watched_item_field_context(MagicMock(), wi, "default_schedule_interval", mode="view")
        assert ctx["field_value"] == "15m"

    def test_interval_empty_renders_blank(self):
        wi = MagicMock()
        wi.default_schedule_config = None
        wi.domain_default_schedule_config = None
        wi.last_checked_at = None
        ctx = _watched_item_field_context(MagicMock(), wi, "default_schedule_interval", mode="view")
        assert ctx["field_value"] == ""

    def test_apply_interval_writes_into_dict(self):
        wi = WatchedItem(archiver_info_item_id=ULID(), name="x")
        _apply_watched_item_field_update(wi, "default_schedule_interval", "30m")
        assert wi.default_schedule_config == {"interval": "30m"}

    def test_apply_interval_rejects_invalid(self):
        wi = WatchedItem(archiver_info_item_id=ULID(), name="x")
        with pytest.raises(ValueError):
            _apply_watched_item_field_update(wi, "default_schedule_interval", "bogus")

    def test_apply_interval_empty_clears(self):
        wi = WatchedItem(
            archiver_info_item_id=ULID(),
            name="x",
            default_schedule_config={"interval": "1h"},
        )
        _apply_watched_item_field_update(wi, "default_schedule_interval", "")
        assert wi.default_schedule_config in (None, {})


class TestFieldRoutes:
    async def test_get_field_partial_view_mode(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="FieldTest")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/field/name",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"FieldTest" in response.content

    async def test_post_field_updates(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Old")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/name",
            data={"value": "New"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.name == "New"

    async def test_post_content_media_type_override(self, client, db_session):
        """#168: the detail-page content_media_type override round-trips and
        recomputes the generated essence + emits a WATCHED_ITEM_UPDATED audit."""
        from sqlalchemy import select

        from src.core.models.audit_log import AuditLog, EventType

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="Override",
            content_media_type="text/html",
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.post(
            f"/watched-items/{wi.id}/field/content_media_type",
            data={"value": "application/pdf"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.content_media_type == "application/pdf"
        assert wi.media_type_essence == "application/pdf"

        audits = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_UPDATED)
                )
            )
            .scalars()
            .all()
        )
        assert any(
            a.payload.get("watched_item_id") == str(wi.id)
            and "content_media_type" in (a.payload.get("updated_fields") or [])
            for a in audits
        )

    async def test_interval_field_partial_shows_active_profile_cadence(self, client, db_session):
        """#206 CR-1: the inline interval field partial honors an active profile
        (· profile), matching the full detail page — not the base cadence."""
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="FieldProfile",
            default_schedule_config={"interval": "1d"},
        )
        db_session.add(wi)
        await db_session.flush()
        db_session.add(
            TemporalProfile(
                watched_item_id=wi.id,
                profile_type=ProfileType.EVENT,
                reference_date=date.today() + timedelta(days=10),
                rules=[{"days_before": 3650, "interval": "1h"}],
                post_action=PostAction.DEACTIVATE,
            )
        )
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/field/default_schedule_interval",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "· profile".encode() in response.content

    async def test_post_interval_updates_jsonb(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Sched")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/default_schedule_interval",
            data={"value": "45m"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.default_schedule_config == {"interval": "45m"}

    async def test_invalid_interval_rejected(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Sched")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/field/default_schedule_interval",
            data={"value": "bogus"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    async def test_unknown_field_400(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="X")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(
            f"/watched-items/{wi.id}/field/nonsense",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400


class TestListScheduleMaps:
    """Unit tests for the list-view schedule map (#204, #205, #206).

    ``_build_schedule_map`` returns one ``ScheduleDisplay`` per item, the single
    source the list template reads for both the Interval and Next Check columns.
    """

    NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)

    def _wi(self, *, item=None, domain=None, last_checked_at=None):
        wi = MagicMock()
        wi.id = ULID()
        wi.default_schedule_config = item
        wi.domain_default_schedule_config = domain
        wi.last_checked_at = last_checked_at
        return wi

    def test_resolves_inherited_default(self):
        wi = self._wi(item=None, domain=None)
        sd = _build_schedule_map([wi], self.NOW)[str(wi.id)]
        assert sd.interval_text == "1d"  # SYSTEM_DEFAULT_SCHEDULE_CONFIG
        assert sd.marker == "default"  # source label (#205)

    def test_resolves_domain_default(self):
        """#205: an item inheriting a domain cadence is marked '· domain', not '· default'."""
        wi = self._wi(item=None, domain={"interval": "7d"})
        sd = _build_schedule_map([wi], self.NOW)[str(wi.id)]
        assert sd.interval_text == "7d"
        assert sd.marker == "domain"

    def test_explicit_not_inherited(self):
        wi = self._wi(item={"interval": "6h"}, domain=None)
        sd = _build_schedule_map([wi], self.NOW)[str(wi.id)]
        assert sd.interval_text == "6h"
        assert sd.marker is None

    def test_empty_config_shows_braces(self):
        wi = self._wi(item={}, domain=None)
        sd = _build_schedule_map([wi], self.NOW)[str(wi.id)]
        assert sd.interval_text == "{ }"
        assert sd.marker is None

    def test_next_check_resolves_inherited_default(self):
        last = self.NOW - timedelta(hours=3)
        wi = self._wi(item=None, domain=None, last_checked_at=last)
        # Inherited interval is 1d → next check = last + 1d.
        sd = _build_schedule_map([wi], self.NOW)[str(wi.id)]
        assert sd.next_check == last + timedelta(days=1)

    def test_next_check_none_when_never_checked(self):
        wi = self._wi(item=None, domain=None, last_checked_at=None)
        assert _build_schedule_map([wi], self.NOW)[str(wi.id)].next_check is None

    def test_active_profile_overrides_interval_and_next_check(self):
        """#206: a currently-active profile drives the displayed interval + next-check,
        matching schedule_tick (the #204 CR finding-2 gap)."""
        last = self.NOW - timedelta(minutes=10)
        wi = self._wi(item={"interval": "1d"}, domain=None, last_checked_at=last)
        profiles = {
            str(wi.id): [
                {
                    "profile_type": "event",
                    "reference_date": "2026-06-24",  # 5 days out from NOW
                    "rules": [{"days_before": 30, "interval": "1h"}],
                    "is_active": True,
                }
            ]
        }
        sd = _build_schedule_map([wi], self.NOW, profiles)[str(wi.id)]
        assert sd.profile_active is True
        assert sd.interval_text == "1h"  # profile cadence, not base 1d
        assert sd.marker == "profile"
        assert sd.next_check == last + timedelta(hours=1)


class TestTagsEditor:
    async def test_get_tags_partial(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="T", default_tags=["a", "b"])
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}/tags", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert b"a" in response.content and b"b" in response.content
        assert b'<span class="chip"><span>' in response.content

    async def test_add_tag(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="T")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/tags",
            data={"tag": "newtag"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert "newtag" in (wi.default_tags or [])

    async def test_remove_tag(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id, name="T", default_tags=["x", "y", "z"]
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.delete(
            f"/watched-items/{wi.id}/tags/y",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        await db_session.refresh(wi)
        assert wi.default_tags == ["x", "z"]

    async def test_mark_reviewed_stamps_now(self, client, db_session):
        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Stamp")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/mark-reviewed", follow_redirects=False
        )
        assert response.status_code in (200, 303)
        await db_session.refresh(wi)
        assert wi.last_reviewed_at is not None


async def _make_wi(db_session, **kwargs):
    """Create + commit a WatchedItem; return it."""

    item = await make_info_item(db_session, name=kwargs.pop("info_name", "PauseWI"))
    wi = WatchedItem(archiver_info_item_id=item.info_item_id, **kwargs)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestPauseResume:
    async def test_pause_active_item(self, client, db_session):
        wi = await _make_wi(db_session, name="ToPause", is_active=True)
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "false"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"Paused" in resp.content
        await db_session.refresh(wi)
        assert wi.is_active is False
        rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_PAUSED)
                )
            )
            .scalars()
            .all()
        )
        events = [r for r in rows if r.payload.get("watched_item_id") == str(wi.id)]
        assert len(events) == 1
        assert events[0].payload["source"] == "dashboard"

    async def test_resume_paused_item(self, client, db_session):
        wi = await _make_wi(db_session, name="ToResume", is_active=False)
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "true"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"Active" in resp.content
        await db_session.refresh(wi)
        assert wi.is_active is True
        rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_RESUMED)
                )
            )
            .scalars()
            .all()
        )
        events = [r for r in rows if r.payload.get("watched_item_id") == str(wi.id)]
        assert len(events) == 1

    async def test_toggle_archived_flashes_and_keeps_state(self, client, db_session):
        wi = await _make_wi(
            db_session, name="ArchToggle", is_active=False, archived_at=datetime.now(UTC)
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "true"},
            headers={"HX-Request": "true"},
        )
        # Guard rejection now re-renders the toggle + OOB flash, not a raw 409.
        assert resp.status_code == 200
        assert b"flash-error" in resp.content
        assert b"archived" in resp.content.lower()
        await db_session.refresh(wi)
        assert wi.is_active is False  # state unchanged

    async def test_toggle_archived_non_htmx_redirects(self, client, db_session):
        wi = await _make_wi(
            db_session, name="ArchToggleRedir", is_active=False, archived_at=datetime.now(UTC)
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_resume_domain_suspended_flashes_and_keeps_state(self, client, db_session):
        wi = await _make_wi(db_session, name="SuspResume", is_active=False, domain_suspended=True)
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "true"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"flash-warning" in resp.content
        assert b"suspended" in resp.content.lower()
        await db_session.refresh(wi)
        assert wi.is_active is False  # resume was rejected

    async def test_detail_has_check_now_sync_region(self, client, db_session):
        """Detail Check-now button is an OOB-swappable region (#202)."""
        wi = await _make_wi(
            db_session, name="CheckSync", is_active=True, effective_url="https://example.com"
        )
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        assert b'id="wi-check-now"' in resp.content

    async def test_detail_toggle_oob_syncs_and_disables_check_now(self, client, db_session):
        """Pausing via the detail toggle OOB-disables the Check-now button (#202)."""
        wi = await _make_wi(
            db_session, name="CheckOOB", is_active=True, effective_url="https://example.com"
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "false"},  # detail toggle: compact unset
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Check-now region is re-sent as an OOB swap…
        assert b'id="wi-check-now"' in resp.content
        assert b'hx-swap-oob="true"' in resp.content
        # …the toggle body reflects the paused state, Check-now is disabled.
        assert b"Paused" in resp.content
        assert b"disabled" in resp.content

    async def test_compact_list_row_toggle_rejection_echoes_toggle_id(self, client, db_session):
        """List-row (compact) guard rejection re-renders with the per-row toggle_id + flash."""
        wi = await _make_wi(db_session, name="RowSusp", is_active=False, domain_suspended=True)
        toggle_id = f"wi-status-{wi.id}"
        resp = await client.post(
            f"/watched-items/{wi.id}/toggle-active",
            data={"active": "true", "toggle_id": toggle_id, "compact": "1"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert f'id="{toggle_id}"'.encode() in resp.content
        assert b"flash-warning" in resp.content

    async def test_toggle_unknown_returns_404(self, client):
        resp = await client.post(f"/watched-items/{ULID()}/toggle-active", data={"active": "false"})
        assert resp.status_code == 404

    async def test_detail_renders_status_toggle_and_check_now(self, client, db_session):
        wi = await _make_wi(
            db_session, name="DetailControls", is_active=True, effective_url="https://example.com"
        )
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        assert b"/toggle-active" in resp.content
        assert b"Check now" in resp.content
        # Change-URL re-probe affordance + read-only source specs panel
        assert b"/effective-url" in resp.content
        assert b"Source Specs" in resp.content

    async def test_detail_renders_source_specs_json(self, client, db_session):
        wi = await _make_wi(
            db_session, name="SpecsView", source_specs=[{"kind": "css", "selector": ".x"}]
        )
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        assert b"selector" in resp.content

    async def test_detail_shows_check_activity(self, client, db_session):
        """WatchedItem detail surfaces check audit activity (#190 — execution visibility)."""

        wi = await _make_wi(db_session, name="ActivityWI", effective_url="https://example.com")
        db_session.add(
            AuditLog(
                event_type=EventType.CHECK_NO_CHANGE,
                payload={"watched_item_id": str(wi.id)},
            )
        )
        await db_session.commit()
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        assert b"Recent Activity" in resp.content
        assert b"Checked \xe2\x80\x94 no change" in resp.content  # em-dash

    async def test_detail_shows_health_when_unknown(self, client, db_session):
        """Health badge + tooltip render inline (bound to Last Checked) even when UNKNOWN (#202)."""
        wi = await _make_wi(db_session, name="UnknownHealth")
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        body = resp.content
        assert b'aria-label="Health: unknown' in body
        assert b'class="badge badge-inactive cursor-help"' in body
        assert b"No successful check has been recorded yet." in body  # tooltip text


class TestCheckNow:
    async def test_check_now_success_queues(self, client, db_session):
        wi = await _make_wi(db_session, name="CheckOK", is_active=True)
        wi.effective_url = "https://example.com"
        await db_session.commit()
        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            resp = await client.post(
                f"/watched-items/{wi.id}/check-now", headers={"HX-Request": "true"}
            )
        assert resp.status_code == 200
        assert b"Check queued" in resp.content
        mock_task.configure.return_value.defer_async.assert_awaited_once()

    async def test_check_now_non_htmx_redirects(self, client, db_session):
        """Non-HTMX clients get a redirect fallback, not a bare flash fragment."""

        wi = await _make_wi(db_session, name="CheckRedir", is_active=True)
        wi.effective_url = "https://example.com"
        await db_session.commit()
        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            resp = await client.post(f"/watched-items/{wi.id}/check-now", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/watched-items/{wi.id}"

    async def test_check_now_paused_flashes_error(self, client, db_session):
        wi = await _make_wi(
            db_session, name="CheckPaused", is_active=False, effective_url="https://example.com"
        )
        resp = await client.post(
            f"/watched-items/{wi.id}/check-now", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert b"flash-error" in resp.content
        assert b"paused" in resp.content.lower()

    async def test_check_now_unknown_returns_404(self, client):
        resp = await client.post(f"/watched-items/{ULID()}/check-now")
        assert resp.status_code == 404
