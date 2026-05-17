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
