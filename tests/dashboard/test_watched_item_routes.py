"""Integration tests for WatchedItem dashboard routes (#185 Phase A step 7)."""

import pytest

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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

    async def test_aspect_review_column_removed(self, client, db_session):
        """Aspect Review column removed from list view (#173)."""
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="HtmxRow")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"aspect-review-status" not in response.content


class TestDetailPage:
    async def test_returns_200(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Detail Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Detail Test" in response.content

    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.get(f"/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_shows_effective_url(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Danger")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content

    async def test_domain_suspended_banner_renders(self, client, db_session):
        """Domain Inactive alert shows when watched_item.domain_suspended=True."""
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.domain import Domain
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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

    async def test_new_watch_button_visible_when_active_with_effective_url(
        self, client, db_session
    ):
        """New Watch button appears on active WI that has effective_url set."""
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id,
            name="Active",
            effective_url="https://example.com/target",
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"+ New Watch" in response.content

    async def test_new_watch_button_hidden_when_archived(self, client, db_session):
        """New Watch button absent on archived WI."""
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="NoPrimary")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"+ New Watch" not in response.content

    async def test_detail_page_with_child_watch_renders_200(self, client, db_session):
        """Regression: health_map must be passed when the WI has child watches."""
        from tests.conftest import make_watch

        watch = await make_watch(db_session, name="Child Watch")
        wi = watch.watched_item
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Child Watch" in response.content

    async def test_child_watch_table_uses_static_headers_not_global_partial(
        self, client, db_session
    ):
        """Regression #182: sort buttons must NOT point at /partials/watch-table."""
        from tests.conftest import make_watch

        watch = await make_watch(db_session, name="Scoped Watch")
        wi = watch.watched_item
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Scoped Watch" in response.content
        assert b'hx-get="/partials/watch-table"' not in response.content

    async def test_detail_page_with_no_child_watches_shows_empty_state(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Empty WI")
        db_session.add(wi)
        await db_session.commit()

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"No watches under this Watched Item" in response.content

    async def test_aspect_review_status_route_gone(self, client, db_session):
        """The /aspect-review-status route was removed in step 7."""
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        db_session.add(
            WatchedItem(archiver_info_item_id=item.info_item_id, name="Cannabis Observer")
        )
        await db_session.flush()
        await db_session.commit()

        response = await client.get("/partials/watched-items-table?q=cannabis")
        assert b"Cannabis Observer" in response.content

    async def test_pagination_returns_page_two(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="ToArchive")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)
        assert response.status_code in (200, 303)

    async def test_archive_cascades_to_child_watches(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item, make_watch

        item = await make_info_item(db_session)
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Parent")
        db_session.add(wi)
        await db_session.flush()
        w = await make_watch(db_session, name="Child", watched_item=wi)
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)

        await db_session.refresh(w)
        assert w.is_archived is True

    async def test_restore_clears_archived_at(self, client, db_session):
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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


class TestFieldHelpers:
    def test_interval_format(self):
        from unittest.mock import MagicMock

        from src.dashboard.routes import _watched_item_field_context

        wi = MagicMock()
        wi.default_schedule_config = {"interval": "15m"}
        ctx = _watched_item_field_context(MagicMock(), wi, "default_schedule_interval", mode="view")
        assert ctx["field_value"] == "15m"

    def test_interval_empty_renders_blank(self):
        from unittest.mock import MagicMock

        from src.dashboard.routes import _watched_item_field_context

        wi = MagicMock()
        wi.default_schedule_config = None
        ctx = _watched_item_field_context(MagicMock(), wi, "default_schedule_interval", mode="view")
        assert ctx["field_value"] == ""

    def test_apply_interval_writes_into_dict(self):
        from ulid import ULID

        from src.core.models.watched_item import WatchedItem
        from src.dashboard.routes import _apply_watched_item_field_update

        wi = WatchedItem(archiver_info_item_id=ULID(), name="x")
        _apply_watched_item_field_update(wi, "default_schedule_interval", "30m")
        assert wi.default_schedule_config == {"interval": "30m"}

    def test_apply_interval_rejects_invalid(self):
        import pytest
        from ulid import ULID

        from src.core.models.watched_item import WatchedItem
        from src.dashboard.routes import _apply_watched_item_field_update

        wi = WatchedItem(archiver_info_item_id=ULID(), name="x")
        with pytest.raises(ValueError):
            _apply_watched_item_field_update(wi, "default_schedule_interval", "bogus")

    def test_apply_interval_empty_clears(self):
        from ulid import ULID

        from src.core.models.watched_item import WatchedItem
        from src.dashboard.routes import _apply_watched_item_field_update

        wi = WatchedItem(
            archiver_info_item_id=ULID(),
            name="x",
            default_schedule_config={"interval": "1h"},
        )
        _apply_watched_item_field_update(wi, "default_schedule_interval", "")
        assert wi.default_schedule_config in (None, {})


class TestFieldRoutes:
    async def test_get_field_partial_view_mode(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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

    async def test_post_interval_updates_jsonb(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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


class TestTagsEditor:
    async def test_get_tags_partial(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

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
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item

    item = await make_info_item(db_session, name=kwargs.pop("info_name", "PauseWI"))
    wi = WatchedItem(archiver_info_item_id=item.info_item_id, **kwargs)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestPauseResume:
    async def test_pause_active_item(self, client, db_session):
        from sqlalchemy import select

        from src.core.models.audit_log import AuditLog, EventType

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
        from sqlalchemy import select

        from src.core.models.audit_log import AuditLog, EventType

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
        from datetime import UTC, datetime

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
        from datetime import UTC, datetime

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
        from ulid import ULID

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

    async def test_detail_shows_health_when_unknown(self, client, db_session):
        """Health row renders even when status is the default UNKNOWN (#190)."""
        wi = await _make_wi(db_session, name="UnknownHealth")
        resp = await client.get(f"/watched-items/{wi.id}")
        assert resp.status_code == 200
        assert b"Health" in resp.content
        assert b"unknown" in resp.content.lower()


class TestCheckNow:
    async def test_check_now_success_queues(self, client, db_session):
        from unittest.mock import AsyncMock, patch

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
        from unittest.mock import AsyncMock, patch

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
        from ulid import ULID

        resp = await client.post(f"/watched-items/{ULID()}/check-now")
        assert resp.status_code == 404
