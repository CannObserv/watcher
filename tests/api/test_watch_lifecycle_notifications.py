"""Tests for watch lifecycle event notifications (created, paused, resumed)."""

from unittest.mock import AsyncMock, patch

import pytest

from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEventType
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration

_PATCH = "src.api.routes.watches.dispatch_event_notifications"
_PATCH_CORE = "src.core.watches.dispatch_event_notifications"


async def _make_wi(db_session, *, name="W", url="https://example.com/p"):
    """Create a WatchedItem with effective_url; return it."""
    item = await make_info_item(db_session, name=name)
    wi = WatchedItem(info_item_id=item.info_item_id, name=name, effective_url=url)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestWatchCreatedNotification:
    async def test_create_watch_dispatches_watch_created_event(self, client, db_session):
        wi = await _make_wi(db_session, name="Notify Watch", url="https://example.com/notify")
        with patch(_PATCH_CORE, new_callable=AsyncMock) as mock_dispatch:
            response = await client.post(
                "/api/v1/watches",
                json={
                    "name": "Notify Watch",
                    "watched_item_id": str(wi.id),
                    "content_type": "html",
                },
            )
        assert response.status_code == 201, response.text
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_CREATED
        assert kwargs["event"].watch_id == response.json()["id"]

    async def test_create_watch_event_includes_name_and_url(self, client, db_session):
        wi = await _make_wi(db_session, name="My Watch", url="https://example.com/page")
        with patch(_PATCH_CORE, new_callable=AsyncMock) as mock_dispatch:
            await client.post(
                "/api/v1/watches",
                json={"name": "My Watch", "watched_item_id": str(wi.id), "content_type": "html"},
            )
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.watch_name == "My Watch"
        assert event.watch_url == "https://example.com/page"


class TestWatchPausedResumedNotifications:
    async def _create_watch(self, client, db_session, *, is_active=True):
        wi = await _make_wi(db_session)
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert resp.status_code == 201, resp.text
        watch_id = resp.json()["id"]
        if not is_active:
            await client.patch(f"/api/v1/watches/{watch_id}", json={"is_active": False})
        return watch_id

    async def test_deactivate_dispatches_watch_paused(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": False},
            )
        assert response.status_code == 200
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_PAUSED

    async def test_reactivate_dispatches_watch_resumed(self, client, db_session):
        watch_id = await self._create_watch(client, db_session, is_active=False)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": True},
            )
        assert response.status_code == 200
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_RESUMED

    async def test_update_non_active_field_does_not_dispatch(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"name": "Renamed"},
            )
        assert response.status_code == 200
        mock_dispatch.assert_not_awaited()

    async def test_set_active_true_when_already_active_does_not_dispatch(self, client, db_session):
        watch_id = await self._create_watch(client, db_session)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": True},
            )
        assert response.status_code == 200
        mock_dispatch.assert_not_awaited()


class TestWatchDeletedNotification:
    async def _create_archived_watch(self, client, db_session):
        from ulid import ULID

        from src.core.models.watch import Watch

        wi = await _make_wi(db_session, name="Delete Watch", url="https://example.com/d")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Delete Watch", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert resp.status_code == 201, resp.text
        watch_id = resp.json()["id"]
        # No archive API endpoint — flip the flags directly.
        watch = await db_session.get(Watch, ULID.from_str(watch_id))
        watch.is_active = False
        watch.is_archived = True
        await db_session.commit()
        return watch_id

    async def test_delete_watch_dispatches_watch_deleted_event(self, client, db_session):
        watch_id = await self._create_archived_watch(client, db_session)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.delete(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 204
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_DELETED
        assert kwargs["event"].watch_id == watch_id

    async def test_delete_event_includes_name_and_url(self, client, db_session):
        watch_id = await self._create_archived_watch(client, db_session)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            await client.delete(f"/api/v1/watches/{watch_id}")
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.watch_name == "Delete Watch"
        assert event.watch_url == "https://example.com/d"
