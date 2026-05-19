"""Integration tests for WatchedItem dashboard routes."""

import httpx
import pytest
from archiver_client import AuthError, NotFound, ServerError

pytestmark = pytest.mark.integration

# Every exception class the watched_item_detail_page route's two SDK
# try/except blocks promise to swallow. Used to parametrise graceful-
# fallback tests so a future refactor splitting the except clauses can't
# silently regress any single path.
_SDK_FAILURE_FACTORIES = [
    pytest.param(lambda: NotFound("nope"), id="NotFound"),
    pytest.param(lambda: ServerError("nope"), id="ServerError"),
    pytest.param(lambda: AuthError("nope"), id="AuthError"),
    pytest.param(lambda: httpx.ConnectError("nope"), id="ConnectError"),
]


class TestListPage:
    async def test_returns_200(self, client):
        response = await client.get("/watched-items")
        assert response.status_code == 200

    async def test_empty_state_renders_cta(self, client):
        response = await client.get("/watched-items")
        body = response.content
        # Empty state copy + CTA to /watches/new
        assert b"No watched items yet" in body
        assert b"/watches/new" in body

    async def test_list_renders_items(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        db_session.add(WatchedItem(info_item_id=item.info_item_id, name="Listed"))
        await db_session.flush()
        await db_session.commit()
        response = await client.get("/watched-items")
        assert b"Listed" in response.content

    async def test_sidebar_link_present(self, client):
        response = await client.get("/")
        assert b'href="/watched-items"' in response.content


class TestDetailPage:
    async def test_returns_200_with_archiver_mock(self, client, db_session, info_client):
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Detail Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        info_client.get_info_item = AsyncMock(
            return_value=_fake_info_item_out(
                info_item_id=str(item.info_item_id),
            )
        )

        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Detail Test" in response.content

    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.get(f"/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_renders_info_item_summary(self, client, db_session, info_client):
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Summary Test")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=_fake_info_item_out(info_item_id=str(item.info_item_id))
        )
        info_client.get_info_source = AsyncMock(
            return_value=_fake_info_source_out(url="https://example.org/foo")
        )
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        # Primary URL still surfaces in the new binding-tree partial.
        assert b"https://example.org/foo" in body
        # Readonly mode: no radio inputs in the tree.
        assert b'type="radio"' not in body

    async def test_binding_list_excludes_primary(self, client, db_session, info_client):
        """Regression: the primary InfoSource is represented by the URL in the
        header; it must not also appear as a row in the binding list (would
        be redundant)."""
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Bindings")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=SimpleNamespace(
                info_item_id=str(item.info_item_id),
                name="Item",
                description=None,
                owner=None,
                info_item_sources=[
                    SimpleNamespace(
                        info_source_id="primary-src-id",
                        role=None,
                        created_at=datetime.now(UTC),
                    ),
                    SimpleNamespace(
                        info_source_id="cross-check-src-id",
                        role="cross_check",
                        created_at=datetime.now(UTC),
                    ),
                ],
            )
        )

        # get_info_source is called per-binding by fetch_info_item_bindings.
        # Side-effect returns the right id so the template renders correctly.
        async def _src(info_source_id):
            return SimpleNamespace(
                info_source_id=info_source_id,
                url="https://example.com/page" if info_source_id == "primary-src-id" else None,
                parent_info_source_id=(
                    None if info_source_id == "primary-src-id" else "primary-src-id"
                ),
                source_spec=SimpleNamespace(additional_properties={}),
            )

        info_client.get_info_source = AsyncMock(side_effect=_src)
        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        # The cross_check row renders.
        assert b"cross-check-src-id" in body
        # The primary's info_source_id does NOT appear in the binding list —
        # the URL stands in for it in the header.
        assert b"primary-src-id" not in body

    @pytest.mark.parametrize("exc_factory", _SDK_FAILURE_FACTORIES)
    async def test_renders_without_primary_url_when_archiver_partial(
        self, client, db_session, info_client, exc_factory
    ):
        """Regression: detail page must render when InfoItem succeeds but
        get_info_source fails. Parametrised across every exception class in
        the route's except clause so a future refactor splitting them can't
        silently regress any single path.

        get_info_source failures propagate as SDK exceptions (ServerError etc.),
        not ValueError, so the outer except clause applies and the route falls
        back to the 'summary unavailable' placeholder.  The ValueError recovery
        path (no primary binding) is covered by
        test_renders_info_item_name_when_no_primary_binding.
        """
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Partial")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=_fake_info_item_out(info_item_id=str(item.info_item_id))
        )
        info_client.get_info_source = AsyncMock(side_effect=exc_factory())
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        # Detail page now uses the picker's binding-tree partial — but
        # when the SDK fails, the page falls back to a placeholder.
        assert b"Information Item summary unavailable" in response.content

    @pytest.mark.parametrize("exc_factory", _SDK_FAILURE_FACTORIES)
    async def test_renders_when_get_info_item_fails(
        self, client, db_session, info_client, exc_factory
    ):
        """Regression: detail page must render with the 'summary unavailable'
        placeholder when get_info_item itself fails. Parametrised across
        every exception class in the route's except clause."""
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Outage")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(side_effect=exc_factory())
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        assert b"Archiver Information Item summary unavailable" in response.content

    async def test_renders_info_item_name_when_no_primary_binding(
        self, client, db_session, info_client
    ):
        """ValueError (no primary binding) must render the InfoItem name in readonly_tree mode,
        not fall back to the 'unavailable' placeholder."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session, name="No-Primary Item")
        wi = WatchedItem(info_item_id=item.info_item_id, name="No-Primary WI")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        # No info_item_sources → fetch_info_item_bindings raises ValueError;
        # route recovers with a second get_info_item call.
        info_client.get_info_item = AsyncMock(
            return_value=SimpleNamespace(
                info_item_id=str(item.info_item_id),
                name="No-Primary Item",
                description=None,
                owner=None,
                info_item_sources=[],
            )
        )
        response = await client.get(f"/watched-items/{wi.id}")
        assert response.status_code == 200
        body = response.text
        assert "No-Primary Item" in body
        assert "unavailable" not in body.lower()
        # readonly_tree injects no radio or hidden info_item_id inputs
        assert 'type="radio"' not in body
        assert 'name="info_item_id"' not in body

    async def test_renders_danger_zone_archive(self, client, db_session, info_client):
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Danger")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=_fake_info_item_out(
                info_item_id=str(item.info_item_id),
            )
        )
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"Danger Zone" in response.content
        assert b"Archive" in response.content

    async def test_new_sub_aspects_get_badge(self, client, db_session, info_client):
        """Sub_aspects created after last_reviewed_at get a 'new' badge in the tree."""
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id,
            name="WI",
            last_reviewed_at=datetime.now(UTC) - timedelta(days=7),
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        # The route's new logic uses fetch_info_item_bindings,
        # which calls get_info_item + get_info_source(per binding). Stub BOTH
        # to return shape-consistent objects so the "new"-badge path is exercised.
        info_client.get_info_item = AsyncMock(
            return_value=SimpleNamespace(
                info_item_id=str(item.info_item_id),
                name="N",
                description=None,
                owner=None,
                info_item_sources=[
                    SimpleNamespace(
                        info_source_id="primary",
                        role=None,
                        created_at=datetime.now(UTC) - timedelta(days=14),
                    ),
                    SimpleNamespace(
                        info_source_id="new-sub",
                        role="sub_aspect",
                        created_at=datetime.now(UTC),
                    ),
                ],
            )
        )

        # get_info_source is called per-binding by fetch_info_item_bindings.
        async def _src(info_source_id):
            if info_source_id == "primary":
                return SimpleNamespace(
                    info_source_id="primary",
                    url="https://example.com",
                    parent_info_source_id=None,
                    source_spec=SimpleNamespace(additional_properties={}),
                )
            return SimpleNamespace(
                info_source_id=info_source_id,
                url=None,
                parent_info_source_id="primary",
                source_spec=SimpleNamespace(additional_properties={}),
            )

        info_client.get_info_source = AsyncMock(side_effect=_src)

        response = await client.get(f"/watched-items/{wi.id}")
        body = response.content
        assert b"new-sub" in body
        # The "new" badge fires because the sub_aspect's created_at is newer
        # than the WatchedItem's last_reviewed_at.
        assert b"badge-warning" in body


class TestArchiveRestore:
    async def test_archive_redirects_back(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="ToArchive")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)
        assert response.status_code in (200, 303)

    async def test_archive_cascades_to_child_watches(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item, make_watch

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Parent")
        db_session.add(wi)
        await db_session.flush()
        w = await make_watch(
            db_session,
            name="Child",
            watched_item=wi,
        )
        await db_session.commit()

        await client.post(f"/watched-items/{wi.id}/archive", follow_redirects=False)

        await db_session.refresh(w)
        assert w.is_archived is True

    async def test_restore_clears_archived_at(self, client, db_session, info_client):
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id,
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

        wi = WatchedItem(info_item_id=ULID(), name="x")
        _apply_watched_item_field_update(wi, "default_schedule_interval", "30m")
        assert wi.default_schedule_config == {"interval": "30m"}

    def test_apply_interval_rejects_invalid(self):
        import pytest
        from ulid import ULID

        from src.core.models.watched_item import WatchedItem
        from src.dashboard.routes import _apply_watched_item_field_update

        wi = WatchedItem(info_item_id=ULID(), name="x")
        with pytest.raises(ValueError):
            _apply_watched_item_field_update(wi, "default_schedule_interval", "bogus")

    def test_apply_interval_empty_clears(self):
        from ulid import ULID

        from src.core.models.watched_item import WatchedItem
        from src.dashboard.routes import _apply_watched_item_field_update

        wi = WatchedItem(
            info_item_id=ULID(),
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="FieldTest")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="Old")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="Sched")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="Sched")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="X")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="T", default_tags=["a", "b"])
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.get(f"/watched-items/{wi.id}/tags", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert b"a" in response.content and b"b" in response.content

    async def test_add_tag(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="T")
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
        wi = WatchedItem(info_item_id=item.info_item_id, name="T", default_tags=["x", "y", "z"])
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

    async def test_add_dedupes(self, client, db_session):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="T", default_tags=["a"])
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        await client.post(
            f"/watched-items/{wi.id}/tags",
            data={"tag": "a"},
            headers={"HX-Request": "true"},
        )
        await db_session.refresh(wi)
        assert wi.default_tags == ["a"]

    @pytest.mark.parametrize("bad_tag", ["foo bar", "foo,bar", "a\tb", "x\ny"])
    async def test_add_rejects_whitespace_or_comma(self, client, db_session, bad_tag):
        """Server-side validation mirrors the HTML5 pattern='[^\\s,]+' so
        non-HTMX callers can't bypass the tag format constraint."""
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="T")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/tags",
            data={"tag": bad_tag},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        await db_session.refresh(wi)
        assert wi.default_tags is None


class TestSubAspectBanner:
    async def test_banner_shows_count_when_new(self, client, db_session, info_client):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        old = datetime.now(UTC) - timedelta(days=10)
        wi = WatchedItem(
            info_item_id=item.info_item_id,
            name="Review",
            last_reviewed_at=old,
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        info_client.get_info_item = AsyncMock(
            return_value=SimpleNamespace(
                info_item_id=str(item.info_item_id),
                name="Has new",
                description=None,
                owner=None,
                info_item_sources=[
                    SimpleNamespace(
                        info_source_id="p",
                        role=None,
                        created_at=datetime.now(UTC) - timedelta(days=15),
                    ),
                    SimpleNamespace(
                        info_source_id="s1", role="sub_aspect", created_at=datetime.now(UTC)
                    ),
                    SimpleNamespace(
                        info_source_id="s2", role="sub_aspect", created_at=datetime.now(UTC)
                    ),
                ],
            )
        )

        # fetch_info_item_bindings calls get_info_source per-binding; stub it so
        # the route can resolve bindings and count_new_subaspects gets a live info_item.
        async def _src(info_source_id):
            return SimpleNamespace(
                info_source_id=info_source_id,
                url="https://example.com" if info_source_id == "p" else None,
                parent_info_source_id=None if info_source_id == "p" else "p",
                source_spec=SimpleNamespace(additional_properties={}),
            )

        info_client.get_info_source = AsyncMock(side_effect=_src)
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"2 new sub_aspects" in response.content

    async def test_no_banner_when_none_new(self, client, db_session, info_client):
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(
            info_item_id=item.info_item_id,
            name="Reviewed",
            last_reviewed_at=datetime.now(UTC),
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        info_client.get_info_item = AsyncMock(
            return_value=SimpleNamespace(
                info_item_id=str(item.info_item_id),
                name="x",
                description=None,
                owner=None,
                info_item_sources=[],
            )
        )
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"new sub_aspects" not in response.content

    async def test_mark_reviewed_stamps_now(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem
        from tests.conftest import make_info_item

        item = await make_info_item(db_session)
        wi = WatchedItem(info_item_id=item.info_item_id, name="Stamp")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        response = await client.post(
            f"/watched-items/{wi.id}/mark-reviewed", follow_redirects=False
        )
        assert response.status_code in (200, 303)
        await db_session.refresh(wi)
        assert wi.last_reviewed_at is not None


def _fake_info_item_out(*, info_item_id):
    """Minimal InfoItemOut-shaped mock for the summary card.

    Matches the real SDK `InfoItemSourceOut` shape — no `url` attribute on
    the source. The detail route resolves the primary URL via a separate
    ``get_info_source(...)`` call; tests that need the URL must stub that
    call too (see :func:`_fake_info_source_out`).
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    return SimpleNamespace(
        info_item_id=info_item_id,
        name="Fake InfoItem",
        description=None,
        owner=None,
        info_item_sources=[
            SimpleNamespace(
                info_source_id="fake-primary-src",
                role=None,  # primary
                created_at=datetime.now(UTC),
            ),
        ],
    )


def _fake_info_source_out(url="https://example.com"):
    """Minimal InfoSourceOut-shaped mock for ``get_info_source`` calls."""
    from types import SimpleNamespace

    return SimpleNamespace(info_source_id="fake-primary-src", url=url)
