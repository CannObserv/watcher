"""Tests for watch lifecycle event notifications (created, paused, resumed)."""

from unittest.mock import AsyncMock, patch

import pytest

from src.core.notifications.events import WatchEventType

pytestmark = pytest.mark.integration

_PATCH = "src.api.routes.watches.dispatch_event_notifications"


class TestWatchCreatedNotification:
    async def test_create_watch_dispatches_watch_created_event(self, client):
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.post(
                "/api/v1/watches",
                json={
                    "name": "Notify Watch",
                    "url": "https://example.com/notify",
                    "content_type": "html",
                },
            )
        assert response.status_code == 201
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_CREATED
        assert kwargs["event"].watch_id == response.json()["id"]

    async def test_create_watch_event_includes_name_and_url(self, client):
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            await client.post(
                "/api/v1/watches",
                json={
                    "name": "My Watch",
                    "url": "https://example.com/page",
                    "content_type": "html",
                },
            )
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.watch_name == "My Watch"
        assert event.watch_url == "https://example.com/page"


class TestWatchPausedResumedNotifications:
    async def _create_watch(self, client, *, is_active=True):
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "url": "https://example.com/p", "content_type": "html"},
        )
        assert resp.status_code == 201
        watch_id = resp.json()["id"]
        if not is_active:
            await client.patch(f"/api/v1/watches/{watch_id}", json={"is_active": False})
        return watch_id

    async def test_deactivate_dispatches_watch_paused(self, client):
        watch_id = await self._create_watch(client)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": False},
            )
        assert response.status_code == 200
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_PAUSED

    async def test_reactivate_dispatches_watch_resumed(self, client):
        watch_id = await self._create_watch(client, is_active=False)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": True},
            )
        assert response.status_code == 200
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        assert kwargs["event"].event_type == WatchEventType.WATCH_RESUMED

    async def test_update_non_active_field_does_not_dispatch(self, client):
        watch_id = await self._create_watch(client)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"name": "Renamed"},
            )
        assert response.status_code == 200
        mock_dispatch.assert_not_awaited()

    async def test_set_active_true_when_already_active_does_not_dispatch(self, client):
        watch_id = await self._create_watch(client)
        with patch(_PATCH, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch_id}",
                json={"is_active": True},
            )
        assert response.status_code == 200
        mock_dispatch.assert_not_awaited()
