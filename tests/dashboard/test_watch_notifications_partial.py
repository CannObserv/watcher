"""Unit tests for the watch notifications partial route and template.

These are pure unit tests — no database required. All context helpers
are mocked with unittest.mock.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from ulid import ULID


def _make_mock_watch(watch_id=None, name="Test Watch"):
    """Build a minimal Watch-like mock."""
    watch = MagicMock()
    watch.id = watch_id or ULID()
    watch.name = name
    return watch


def _make_mock_nc(
    nc_id=None,
    channel_hint="slack",
    events=None,
    is_active=True,
):
    """Build a minimal NotificationConfig-like mock."""
    nc = MagicMock()
    nc.id = nc_id or ULID()
    nc.channel_hint = channel_hint
    nc.events = events if events is not None else ["change_detected"]
    nc.is_active = is_active
    return nc


class TestWatchNotificationsPartialRoute:
    """GET /partials/watch-notifications/{watch_id}"""

    async def _get(self, watch_id: str, mock_watch=None, mock_notifications=None):
        """Make request to the partial endpoint with mocked dependencies."""
        from src.api.dependencies import get_db_session
        from src.api.main import app

        # Provide a dummy session — the route's context calls are patched
        async def override_session():
            yield MagicMock()

        app.dependency_overrides[get_db_session] = override_session

        try:
            with (
                patch(
                    "src.dashboard.routes.get_watch_detail",
                    new_callable=AsyncMock,
                    return_value=mock_watch,
                ) as _mock_get_watch,
                patch(
                    "src.dashboard.routes.get_watch_notifications",
                    new_callable=AsyncMock,
                    return_value=mock_notifications or [],
                ) as _mock_get_notifications,
            ):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get(f"/partials/watch-notifications/{watch_id}")
        finally:
            app.dependency_overrides.clear()

    async def test_returns_200_for_valid_watch(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200

    async def test_returns_404_for_unknown_watch(self):
        resp = await self._get("01ZZZZZZZZZZZZZZZZZZZZZZZZ", mock_watch=None)
        assert resp.status_code == 404

    async def test_renders_channel_hint(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(channel_hint="discord")
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"discord" in resp.content

    async def test_renders_events_as_chips(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(events=["change_detected", "watch_error"])
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"change_detected" in resp.content
        assert b"watch_error" in resp.content

    async def test_renders_inactive_badge_when_not_active(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=False)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"Inactive" in resp.content

    async def test_no_inactive_badge_when_active(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        # "Inactive" badge should not appear for active configs
        # (The word may appear in aria labels etc., so check the badge class)
        assert b"badge-inactive" not in resp.content

    async def test_empty_list_shows_empty_state(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[])
        assert resp.status_code == 200
        # No notification cards — no channel_hint present
        assert b"channel_hint" not in resp.content

    async def test_add_form_present(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b"apprise_url" in resp.content

    async def test_event_checkboxes_present(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b"change_detected" in resp.content
        assert b"watch_error" in resp.content
        assert b"watch_resumed" in resp.content
