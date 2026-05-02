"""Unit tests for the watch notifications partial route and template.

These are pure unit tests — no database required. All context helpers
are mocked with unittest.mock.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from ulid import ULID

from src.api.deps import require_api_key
from src.core.models.app_user import AppUser
from src.dashboard.deps import get_dashboard_user

_AUTH_OVERRIDES = {
    get_dashboard_user: lambda: AppUser(id="test-user-id", email="test@example.com"),
    require_api_key: lambda: "test-user-id",
}


def _make_mock_session():
    """Build a MagicMock session where execute() is an AsyncMock returning empty results."""
    session = MagicMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=execute_result)
    return session


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
    apprise_url="encrypted_token",
    title=None,
):
    """Build a minimal NotificationConfig-like mock."""
    nc = MagicMock()
    nc.id = nc_id or ULID()
    nc.channel_hint = channel_hint
    nc.events = events if events is not None else ["change_detected"]
    nc.is_active = is_active
    nc.apprise_url = apprise_url
    nc.title = title
    nc.content_config = None
    return nc


class TestWatchNotificationsPartialRoute:
    """GET /partials/watch-notifications/{watch_id}"""

    async def _get(self, watch_id: str, mock_watch=None, mock_notifications=None):
        """Make request to the partial endpoint with mocked dependencies."""
        from src.api.deps import get_db_session
        from src.api.main import app

        # Provide a dummy session — the route's context calls are patched
        async def override_session():
            yield _make_mock_session()

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides.update(_AUTH_OVERRIDES)

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
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_dashboard_user, None)
            app.dependency_overrides.pop(require_api_key, None)

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
        assert b"badge-inactive" not in resp.content

    async def test_active_badge_when_active(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"badge-active" in resp.content

    async def test_empty_list_shows_empty_state(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[])
        assert resp.status_code == 200
        # No notification cards — no channel_hint present
        assert b"channel_hint" not in resp.content

    async def test_empty_state_row_has_id(self):
        """Empty-state <tr> has id='notifications-empty-state' so JS can remove it."""
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[])
        assert resp.status_code == 200
        assert b'id="notifications-empty-state"' in resp.content

    async def test_renders_table_structure(self):
        """Notification list renders as a <table> with thead/tbody."""
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b"<table" in resp.content
        assert b"<thead" in resp.content
        assert b'id="notifications-tbody"' in resp.content

    async def test_no_add_form_in_partial(self):
        """Add form is no longer embedded in the partial — it lives in the add-row route."""
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b"add-notification-form" not in resp.content
        assert b"channel-picker" not in resp.content

    async def test_pause_button_for_active_config(self):
        """Active notification config shows a Pause toggle button."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"Pause" in resp.content

    async def test_activate_button_for_inactive_config(self):
        """Inactive notification config shows an Activate toggle button."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=False)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"Activate" in resp.content

    async def test_no_deactivate_button_label(self):
        """'Deactivate' label is replaced by 'Pause' — must not appear."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"Deactivate" not in resp.content

    async def test_button_order_edit_test_pause_delete(self):
        """Action buttons appear in order: Edit, Test, Pause/Activate, Delete."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        text = resp.text
        edit_pos = text.index(f"/notifications/{nc.id}/edit")  # Edit link href
        test_pos = text.index("test-result")  # hx-post URL contains "test-result"
        pause_pos = text.index("/toggle")  # hx-post URL contains "/toggle"
        delete_pos = text.index("hx-delete")  # delete uses hx-delete
        assert edit_pos < test_pos < pause_pos < delete_pos

    async def test_edit_button_present_for_each_config(self):
        """Edit button links to the dedicated edit page for the config."""
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert f"/notifications/{nc.id}/edit".encode() in resp.content
        assert b"Edit" in resp.content

    async def test_renders_title_when_set(self):
        """Title appears in the notification row when the nc has a non-null title."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(title="Slack ops channel")
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="json://x.com"):
            resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"Slack ops channel" in resp.content

    async def test_falls_back_to_channel_hint_when_no_title(self):
        """When title is None, the channel_hint is still visible."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(channel_hint="discord", title=None)
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="json://x.com"):
            resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"discord" in resp.content


# ---------------------------------------------------------------------------
# Helper for POST mutations
# ---------------------------------------------------------------------------


async def _post_dashboard(
    path: str, form_data=None, mock_watch=None, mock_notifications=None, mock_session=None
):
    from src.api.deps import get_db_session
    from src.api.main import app

    _session = mock_session or _make_mock_session()

    async def override_session():
        yield _session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides.update(_AUTH_OVERRIDES)
    try:
        with (
            patch(
                "src.dashboard.routes.get_watch_detail",
                new_callable=AsyncMock,
                return_value=mock_watch,
            ),
            patch(
                "src.dashboard.routes.get_watch_notifications",
                new_callable=AsyncMock,
                return_value=mock_notifications or [],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(path, data=form_data or {})
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_dashboard_user, None)
        app.dependency_overrides.pop(require_api_key, None)


async def _get_dashboard(path: str, mock_watch=None, mock_session=None):
    """Make an authenticated GET to a dashboard route with mocked dependencies.

    Patches get_watch_detail with mock_watch and injects mock_session via
    the DB dependency override (session.get, session.commit etc. are all set
    on the mock before calling this helper).
    """
    from src.api.deps import get_db_session
    from src.api.main import app

    _session = mock_session or _make_mock_session()

    async def override_session():
        yield _session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides.update(_AUTH_OVERRIDES)
    try:
        with patch(
            "src.dashboard.routes.get_watch_detail",
            new_callable=AsyncMock,
            return_value=mock_watch,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path)
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_dashboard_user, None)
        app.dependency_overrides.pop(require_api_key, None)


class TestWatchNotificationToggleRoute:
    """POST /watches/{watch_id}/notifications/{config_id}/toggle"""

    async def test_returns_200_and_partial(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        nc.watch_id = watch.id  # ownership check passes
        session = _make_mock_session()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        resp = await _post_dashboard(
            f"/watches/{watch.id}/notifications/{nc.id}/toggle",
            mock_watch=watch,
            mock_session=session,
        )
        assert resp.status_code == 200
        assert b"notifications-tbody" in resp.content

    async def test_returns_404_for_missing_watch(self):
        resp = await _post_dashboard(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/notifications/01ZZZZZZZZZZZZZZZZZZZZZZZZ/toggle",
            mock_watch=None,
        )
        assert resp.status_code == 404

    async def test_returns_404_for_config_belonging_to_different_watch(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = MagicMock()  # different ID — won't equal watch.id
        session = _make_mock_session()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        resp = await _post_dashboard(
            f"/watches/{watch.id}/notifications/{nc.id}/toggle",
            mock_watch=watch,
            mock_session=session,
        )
        assert resp.status_code == 404


class TestWatchNotificationCreateRoute:
    """POST /watches/{watch_id}/notifications/new"""

    async def test_invalid_url_returns_200_with_error(self):
        watch = _make_mock_watch()
        resp = await _post_dashboard(
            f"/watches/{watch.id}/notifications/new",
            form_data={"apprise_url": "notascheme://bad", "events": "change_detected"},
            mock_watch=watch,
        )
        assert resp.status_code == 200
        assert (
            b"Invalid" in resp.content
            or b"invalid" in resp.content
            or b"error" in resp.content.lower()
        )

    async def test_missing_watch_returns_404(self):
        resp = await _post_dashboard(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/notifications/new",
            form_data={"apprise_url": "json://hooks.example.com", "events": "change_detected"},
            mock_watch=None,
        )
        assert resp.status_code == 404


class TestWatchNotificationTestResultRoute:
    """POST /watches/{watch_id}/notifications/{config_id}/test-result"""

    def _mock_result(self, success=True, reason="ok"):
        from src.core.notifications.dispatcher import DispatchResult

        return DispatchResult(success=success, reason=reason)

    async def test_success_returns_flash_with_reason(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = watch.id
        session = _make_mock_session()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        result = self._mock_result(True, "Notification sent successfully")
        with patch(
            "src.dashboard.routes.dispatch_event", new_callable=AsyncMock, return_value=result
        ) as mock_dispatch:
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/test-result",
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"flash-region" in resp.content
        assert b"flash-success" in resp.content
        assert b"Notification sent successfully" in resp.content
        mock_dispatch.assert_called_once()
        _, kwargs = mock_dispatch.call_args
        assert "title" in kwargs and kwargs["title"]
        assert "body" in kwargs and kwargs["body"]

    async def test_failure_returns_flash_with_reason(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = watch.id
        session = _make_mock_session()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        result = self._mock_result(False, "Delivery failed")
        with patch(
            "src.dashboard.routes.dispatch_event", new_callable=AsyncMock, return_value=result
        ):
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/test-result",
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"flash-region" in resp.content
        assert b"flash-error" in resp.content
        assert b"Delivery failed" in resp.content

    async def test_missing_watch_returns_404(self):
        resp = await _post_dashboard(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/notifications/01ZZZZZZZZZZZZZZZZZZZZZZZZ/test-result",
            mock_watch=None,
        )
        assert resp.status_code == 404


class TestWatchNotificationCreateFromTokens:
    """POST /watches/{id}/notifications/new with schema+tokens payload."""

    async def _post_token_form(
        self,
        watch_id: str,
        schema: str,
        token_fields: dict,
        mock_watch=None,
        events=None,
    ):
        from src.api.deps import get_db_session
        from src.api.main import app

        _session = _make_mock_session()
        _session.commit = AsyncMock()

        async def override_session():
            yield _session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides.update(_AUTH_OVERRIDES)
        try:
            with (
                patch(
                    "src.dashboard.routes.get_watch_detail",
                    new_callable=AsyncMock,
                    return_value=mock_watch or _make_mock_watch(),
                ),
                patch(
                    "src.dashboard.routes.get_watch_notifications",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch("src.dashboard.routes.encrypt_apprise_url", return_value="encrypted"),
                patch("src.dashboard.routes.assemble_url", return_value=f"{schema}://assembled"),
            ):
                form_data = {"plugin_schema": schema, **(events or {})}
                form_data.update({f"tok_{k}": v for k, v in token_fields.items()})
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.post(
                        f"/watches/{watch_id}/notifications/new",
                        data=form_data,
                    )
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_dashboard_user, None)
            app.dependency_overrides.pop(require_api_key, None)

    async def test_token_form_submission_returns_200(self):
        watch = _make_mock_watch()
        resp = await self._post_token_form(
            str(watch.id),
            "discord",
            {"webhook_id": "abc123", "webhook_token": "xyz789"},
            mock_watch=watch,
        )
        assert resp.status_code == 200

    async def test_unknown_schema_shows_error(self):
        watch = _make_mock_watch()
        from src.api.deps import get_db_session
        from src.api.main import app

        async def override_session():
            yield MagicMock()

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides.update(_AUTH_OVERRIDES)
        try:
            with (
                patch(
                    "src.dashboard.routes.get_watch_detail",
                    new_callable=AsyncMock,
                    return_value=watch,
                ),
                patch(
                    "src.dashboard.routes.get_watch_notifications",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch(
                    "src.dashboard.routes.assemble_url",
                    side_effect=ValueError("Unknown Apprise plugin schema"),
                ),
            ):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(
                        f"/watches/{watch.id}/notifications/new",
                        data={"plugin_schema": "notaschema", "tok_x": "y"},
                    )
            assert "Unknown" in resp.text
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_dashboard_user, None)
            app.dependency_overrides.pop(require_api_key, None)


class TestNotificationHtmxPartial:
    """Tests for the watch-notifications partial HTMX refresh trigger."""

    async def test_refresh_trigger_targets_container_not_list(self):
        """The refreshNotifications trigger div must target #watch-notifications."""
        watch = _make_mock_watch()

        from src.api.deps import get_db_session
        from src.api.main import app

        async def override_session():
            yield _make_mock_session()

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides.update(_AUTH_OVERRIDES)
        try:
            with (
                patch(
                    "src.dashboard.routes.get_watch_detail",
                    new_callable=AsyncMock,
                    return_value=watch,
                ),
                patch(
                    "src.dashboard.routes.get_watch_notifications",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
            ):
                from httpx import ASGITransport, AsyncClient

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.get(f"/partials/watch-notifications/{watch.id}")
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_dashboard_user, None)
            app.dependency_overrides.pop(require_api_key, None)

        assert resp.status_code == 200
        # The refresh trigger div must target the outer container, not the list div
        trigger_section = resp.text.split("refreshNotifications from:body")[1]
        assert 'hx-target="#watch-notifications"' in trigger_section
        assert 'hx-target="#watch-notifications-list"' not in trigger_section
