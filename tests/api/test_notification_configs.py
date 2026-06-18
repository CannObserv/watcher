"""Integration tests for notification config API endpoints (remote-channel only).

After Phase 5 (#137), notification configs are pure remote-channel pointers.
Apprise URLs are no longer accepted, validated, or stored.
"""

import pytest
from sqlalchemy import text
from ulid import ULID

from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

VALID_CHANNEL_ID = str(ULID())


_watched_item_counter = 0


async def _make_watched_item_id(db_session):
    global _watched_item_counter
    _watched_item_counter += 1
    wi = await make_watched_item(
        db_session,
        name=f"Test Watched Item {_watched_item_counter}",
        primary_url=f"https://example-{_watched_item_counter}.com",
        default_content_type="html",
    )
    await db_session.commit()
    return str(wi.id)


def _create_payload(**overrides):
    payload = {
        "remote_channel_id": str(ULID()),
        "channel_hint": "json",
        "events": ["change_detected"],
    }
    payload.update(overrides)
    return payload


class TestCreateNotificationConfig:
    async def test_create_with_valid_channel_id(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_hint"] == "json"
        assert data["events"] == ["change_detected"]
        assert data["is_active"] is True
        assert "apprise_url" not in data

    async def test_omitted_content_config_persists_sql_null(self, client, db_session):
        """Omitting content_config stores SQL NULL, not JSONB 'null' (#198)."""
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        assert resp.status_code == 201, resp.text
        config_id = resp.json()["id"]
        is_sql_null = (
            await db_session.execute(
                text(
                    "SELECT content_config IS NULL FROM watch_notification_configs WHERE id = :id"
                ),
                {"id": config_id},
            )
        ).scalar_one()
        assert is_sql_null is True

    async def test_create_with_title(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(title="Slack ops"),
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "Slack ops"

    async def test_create_without_title_defaults_to_null(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        assert resp.status_code == 201
        assert resp.json()["title"] is None

    async def test_create_title_max_100_chars(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(title="x" * 101),
        )
        assert resp.status_code == 422

    async def test_create_default_event(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json={"remote_channel_id": str(ULID())},
        )
        assert resp.status_code == 201
        assert resp.json()["events"] == ["change_detected"]

    async def test_missing_remote_channel_id_returns_422(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json={"events": ["change_detected"]},
        )
        assert resp.status_code == 422

    async def test_empty_events_returns_422(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(events=[]),
        )
        assert resp.status_code == 422

    async def test_unknown_event_returns_422(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(events=["nonexistent_event"]),
        )
        assert resp.status_code == 422

    async def test_invalid_watched_item_returns_404(self, client, db_session):
        resp = await client.post(
            "/api/v1/watched-items/00000000000000000000000000/notifications",
            json=_create_payload(),
        )
        assert resp.status_code == 404

    async def test_multiple_events(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(events=["change_detected", "watch_error"]),
        )
        assert resp.status_code == 201
        assert set(resp.json()["events"]) == {"change_detected", "watch_error"}


class TestListNotificationConfigs:
    async def test_list_returns_all_configs(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(events=["change_detected"]),
        )
        await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(events=["watch_error"]),
        )
        resp = await client.get(f"/api/v1/watched-items/{watch_id}/notifications")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_list_excludes_other_watched_item_configs(self, client, db_session):
        watch_a = await _make_watched_item_id(db_session)
        watch_b = await _make_watched_item_id(db_session)
        await client.post(
            f"/api/v1/watched-items/{watch_a}/notifications",
            json=_create_payload(),
        )
        resp = await client.get(f"/api/v1/watched-items/{watch_b}/notifications")
        assert resp.json() == []


class TestPatchNotificationConfig:
    async def test_patch_title(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(title="Original"),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"title": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated"

    async def test_patch_title_to_null(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(title="Remove me"),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"title": None},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] is None

    async def test_toggle_is_active(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_update_events(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"events": ["watch_error", "watch_recovered"]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["events"]) == {"watch_error", "watch_recovered"}

    async def test_patch_invalid_event_type_returns_422(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"events": ["bad_event"]},
        )
        assert resp.status_code == 422

    async def test_patch_wrong_watched_item_returns_404(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        other_watch_id = await _make_watched_item_id(db_session)
        resp = await client.patch(
            f"/api/v1/watched-items/{other_watch_id}/notifications/{config_id}",
            json={"is_active": False},
        )
        assert resp.status_code == 404

    async def test_patch_remote_channel_id(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        new_channel_id = str(ULID())
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"remote_channel_id": new_channel_id, "channel_hint": "slack"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["remote_channel_id"] == new_channel_id
        assert data["channel_hint"] == "slack"

    async def test_patch_empty_events_returns_422(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
            json={"events": []},
        )
        assert resp.status_code == 422


class TestDeleteNotificationConfig:
    async def test_delete_config(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/watched-items/{watch_id}/notifications/{config_id}")
        assert resp.status_code == 204

    async def test_delete_wrong_watched_item_returns_404(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        create_resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        config_id = create_resp.json()["id"]
        other = await _make_watched_item_id(db_session)
        resp = await client.delete(f"/api/v1/watched-items/{other}/notifications/{config_id}")
        assert resp.status_code == 404


@pytest.mark.integration
async def test_notification_config_has_content_config_column(db_session):
    """ORM model exposes content_config field."""
    from src.core.models.notification_config import WatchNotificationConfig

    wi = await make_watched_item(db_session, name="Test Watch")
    config = WatchNotificationConfig(
        watched_item_id=wi.id,
        channel_hint="slack",
        events=["change_detected"],
        remote_channel_id=str(ULID()),
    )
    db_session.add(config)
    await db_session.flush()
    assert config.content_config is None  # default null


class TestTestNotificationConfig:
    async def _make_config(self, client, watch_id):
        resp = await client.post(
            f"/api/v1/watched-items/{watch_id}/notifications",
            json=_create_payload(),
        )
        return resp.json()["id"]

    def _mock_result(self, success=True, reason="ok"):
        from src.core.notifications.notify import DispatchResult

        return DispatchResult(success=success, reason=reason)

    def _patch_notifier_client(self, monkeypatch=None):
        from unittest.mock import AsyncMock, patch

        client_mock = AsyncMock()
        client_mock.__aenter__ = AsyncMock(return_value=client_mock)
        client_mock.__aexit__ = AsyncMock(return_value=False)
        return patch(
            "src.api.routes.notification_configs.get_notifier_client",
            return_value=client_mock,
        )

    async def test_test_sends_notification_and_returns_success(self, client, db_session):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watched_item_id(db_session)
        config_id = await self._make_config(client, watch_id)
        with (
            patch(
                "src.api.routes.notification_configs.dispatch_via_notifier",
                new_callable=AsyncMock,
                return_value=self._mock_result(True, "Notification sent successfully"),
            ),
            self._patch_notifier_client(),
        ):
            resp = await client.post(
                f"/api/v1/watched-items/{watch_id}/notifications/{config_id}/test"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "reason" in data

    async def test_test_returns_success_false_on_dispatch_failure(self, client, db_session):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watched_item_id(db_session)
        config_id = await self._make_config(client, watch_id)
        with (
            patch(
                "src.api.routes.notification_configs.dispatch_via_notifier",
                new_callable=AsyncMock,
                return_value=self._mock_result(False, "Delivery failed"),
            ),
            self._patch_notifier_client(),
        ):
            resp = await client.post(
                f"/api/v1/watched-items/{watch_id}/notifications/{config_id}/test"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "reason" in data

    async def test_test_returns_404_for_unknown_config(self, client, db_session):
        watch_id = await _make_watched_item_id(db_session)
        fake_id = "01JNVAJNVAJNVAJNVAJNVAJNVA"
        resp = await client.post(f"/api/v1/watched-items/{watch_id}/notifications/{fake_id}/test")
        assert resp.status_code == 404

    async def test_test_returns_404_for_wrong_watched_item(self, client, db_session):
        from unittest.mock import AsyncMock, patch

        watch_id = await _make_watched_item_id(db_session)
        other_id = await _make_watched_item_id(db_session)
        config_id = await self._make_config(client, watch_id)
        with (
            patch(
                "src.api.routes.notification_configs.dispatch_via_notifier",
                new_callable=AsyncMock,
                return_value=self._mock_result(True),
            ),
            self._patch_notifier_client(),
        ):
            resp = await client.post(
                f"/api/v1/watched-items/{other_id}/notifications/{config_id}/test"
            )
        assert resp.status_code == 404

    async def test_test_uses_sentinel_url_when_no_effective_url(self, client, db_session):
        """When watched_item.effective_url is empty, test notification uses watch:id sentinel."""
        from src.core.models.notification_config import WatchNotificationConfig

        wi = await make_watched_item(db_session, name="NoURL", primary_url="https://example.com")
        wi.effective_url = ""
        nc = WatchNotificationConfig(
            watched_item_id=wi.id,
            channel_hint="json",
            events=["change_detected"],
            remote_channel_id=str(ULID()),
        )
        db_session.add(nc)
        await db_session.commit()

        resp = await client.post(f"/api/v1/watched-items/{wi.id}/notifications/{nc.id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data


@pytest.mark.integration
async def test_create_config_with_content_config(client, db_session):
    """content_config round-trips through create → response."""
    watch_id = await _make_watched_item_id(db_session)
    resp = await client.post(
        f"/api/v1/watched-items/{watch_id}/notifications",
        json=_create_payload(
            content_config={
                "default": {"include_diff_snippet": True, "diff_snippet_lines": 5},
            },
        ),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content_config"]["default"]["include_diff_snippet"] is True
    assert data["content_config"]["default"]["diff_snippet_lines"] == 5


@pytest.mark.integration
async def test_patch_config_updates_content_config(client, db_session):
    """PATCH with content_config updates the stored value."""
    watch_id = await _make_watched_item_id(db_session)
    create_resp = await client.post(
        f"/api/v1/watched-items/{watch_id}/notifications",
        json=_create_payload(),
    )
    assert create_resp.status_code == 201
    config_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/watched-items/{watch_id}/notifications/{config_id}",
        json={
            "content_config": {
                "default": {"include_diff_full": True},
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["content_config"]["default"]["include_diff_full"] is True
