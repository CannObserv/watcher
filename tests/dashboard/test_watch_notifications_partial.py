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
    apprise_url="encrypted_token",
):
    """Build a minimal NotificationConfig-like mock."""
    nc = MagicMock()
    nc.id = nc_id or ULID()
    nc.channel_hint = channel_hint
    nc.events = events if events is not None else ["change_detected"]
    nc.is_active = is_active
    nc.apprise_url = apprise_url
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
        assert b"Change Detected" in resp.content
        assert b"Watch Error" in resp.content

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
        assert b"add-notification-form" in resp.content
        assert b"channel-picker" in resp.content

    async def test_builder_manual_segment_toggle_present(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b'value="builder"' in resp.content
        assert b'value="manual"' in resp.content
        assert b"segment-group" in resp.content

    async def test_event_checkboxes_present(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        assert b"change_detected" in resp.content
        assert b"watch_error" in resp.content
        assert b"watch_recovered" in resp.content
        assert b"watch_created" in resp.content
        assert b"watch_paused" in resp.content
        assert b"watch_resumed" in resp.content

    async def test_watch_created_checkbox_is_disabled(self):
        watch = _make_mock_watch()
        resp = await self._get(str(watch.id), mock_watch=watch)
        assert resp.status_code == 200
        # "disabled" attr is on the watch_created input specifically
        wc_input = resp.text.split('value="watch_created"')[1].split(">")[0]
        assert "disabled" in wc_input
        # No hidden input when watch_created is not in form_events
        assert '<input type="hidden" name="events" value="watch_created">' not in resp.text

    def test_watch_created_hidden_input_when_previously_set(self):
        """Template renders hidden input to preserve watch_created when form_events includes it."""
        from src.dashboard import templates

        watch = _make_mock_watch()
        rendered = templates.get_template("partials/watch_notifications.html").render(
            request=MagicMock(),
            watch=watch,
            notifications=[],
            form_events=["watch_created", "change_detected"],
        )
        assert '<input type="hidden" name="events" value="watch_created">' in rendered
        # "checked" attr is on the checkbox (last occurrence of value="watch_created"),
        # between that attribute and "disabled"
        wc_attrs = rendered.split('value="watch_created"')[-1].split("disabled")[0]
        assert "checked" in wc_attrs

    async def test_renders_decrypted_url_in_details(self):
        """Notification list reveals decrypted URL via <details> element."""
        watch = _make_mock_watch()
        nc = _make_mock_nc(apprise_url="some_fernet_token")
        with patch(
            "src.dashboard.routes.decrypt_apprise_url", return_value="discord://abc/def/ghi"
        ):
            resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert resp.status_code == 200
        assert b"discord://abc/def/ghi" in resp.content
        assert b"Show URL" in resp.content

    async def test_url_reveal_uses_details_element(self):
        """Show URL toggle uses native <details> element for accessibility."""
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="slack://T/A/T"):
            resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
        assert b"<details" in resp.content
        assert b"Show URL" in resp.content


# ---------------------------------------------------------------------------
# Helper for POST mutations
# ---------------------------------------------------------------------------


async def _post_dashboard(
    path: str, form_data=None, mock_watch=None, mock_notifications=None, mock_session=None
):
    from src.api.dependencies import get_db_session
    from src.api.main import app

    _session = mock_session or MagicMock()

    async def override_session():
        yield _session

    app.dependency_overrides[get_db_session] = override_session
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
        app.dependency_overrides.clear()


class TestWatchNotificationToggleRoute:
    """POST /watches/{watch_id}/notifications/{config_id}/toggle"""

    async def test_returns_200_and_partial(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(is_active=True)
        nc.watch_id = watch.id  # ownership check passes
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        resp = await _post_dashboard(
            f"/watches/{watch.id}/notifications/{nc.id}/toggle",
            mock_watch=watch,
            mock_session=session,
        )
        assert resp.status_code == 200
        assert b"watch-notifications-list" in resp.content

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
        session = MagicMock()
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

    async def test_valid_url_returns_200_with_partial(self):
        watch = _make_mock_watch()
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        with patch("src.dashboard.routes.encrypt_apprise_url", return_value="encrypted"):
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/new",
                form_data={"apprise_url": "json://hooks.example.com", "events": "change_detected"},
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"watch-notifications-list" in resp.content

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
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        result = self._mock_result(True, "Notification sent successfully")
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
        assert b"flash-success" in resp.content
        assert b"Notification sent successfully" in resp.content

    async def test_failure_returns_flash_with_reason(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = watch.id
        session = MagicMock()
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
        from src.api.dependencies import get_db_session
        from src.api.main import app

        _session = MagicMock()
        _session.commit = AsyncMock()

        async def override_session():
            yield _session

        app.dependency_overrides[get_db_session] = override_session
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
            app.dependency_overrides.clear()

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
        from src.api.dependencies import get_db_session
        from src.api.main import app

        async def override_session():
            yield MagicMock()

        app.dependency_overrides[get_db_session] = override_session
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
            app.dependency_overrides.clear()
