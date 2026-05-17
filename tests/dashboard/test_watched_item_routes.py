"""Integration tests for WatchedItem dashboard routes."""

import pytest

pytestmark = pytest.mark.integration


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
            return_value=_fake_info_item_out(
                info_item_id=str(item.info_item_id),
                primary_url="https://example.org/foo",
            )
        )
        response = await client.get(f"/watched-items/{wi.id}")
        assert b"https://example.org/foo" in response.content

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
            info_item_id=wi.info_item_id,
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


def _fake_info_item_out(*, info_item_id, primary_url="https://example.com"):
    """Minimal InfoItemOut-shaped mock for the summary card."""
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
                url=primary_url,
            ),
        ],
    )
