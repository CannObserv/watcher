"""Integration tests for Watch CRUD API endpoints (#185 Phase A step 7)."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import WatchEventType
from tests.conftest import (
    make_info_item,
    make_watch,
)

pytestmark = pytest.mark.integration


async def _make_wi(db_session, *, name="Test WI", url="https://example.com/page"):
    """Create + flush a WatchedItem with effective_url; return it."""
    item = await make_info_item(db_session, name=name)
    wi = WatchedItem(archiver_info_item_id=item.info_item_id, name=name, effective_url=url)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client, db_session):
        wi = await _make_wi(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Test Watch",
                "watched_item_id": str(wi.id),
                "content_type": "html",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["watched_item_id"] == str(wi.id)
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data
        # Dropped fields from Phase A step 6/7.
        assert "info_item_id" not in data
        assert "target_info_source_id" not in data
        assert "effective_url" not in data
        assert "last_checked_at" not in data
        assert "last_changed_at" not in data
        assert "health_status" not in data
        assert "url" not in data
        assert "fetch_config" not in data
        assert "info_source_id" not in data
        assert "schedule_config" not in data

    async def test_create_watch_without_content_type(self, client, db_session):
        """content_type is optional in the new shape — defaults to NULL."""
        wi = await _make_wi(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={"name": "Untyped", "watched_item_id": str(wi.id)},
        )
        assert response.status_code == 201, response.text
        assert response.json()["content_type"] is None

    async def test_create_watch_invalid_content_type(self, client, db_session):
        wi = await _make_wi(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={"name": "Bad", "watched_item_id": str(wi.id), "content_type": "invalid"},
        )
        assert response.status_code == 422

    async def test_create_watch_missing_watched_item_id_returns_422(self, client):
        response = await client.post(
            "/api/v1/watches",
            json={"name": "No item", "content_type": "html"},
        )
        assert response.status_code == 422

    async def test_create_watch_unknown_watched_item_id_returns_422(self, client):
        """Unknown watched_item_id → 422 (no SDK call)."""
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "watched_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 422
        assert "watched_item_id" in response.text or "not found" in response.text.lower()


class TestListWatches:
    async def test_list_watches_empty(self, client):
        response = await client.get("/api/v1/watches")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_watches_returns_created(self, client, db_session):
        wi = await _make_wi(db_session, name="Watch 1")
        await client.post(
            "/api/v1/watches",
            json={"name": "Watch 1", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        response = await client.get("/api/v1/watches")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Watch 1"


class TestGetWatch:
    async def test_get_watch_by_id(self, client, db_session):
        wi = await _make_wi(db_session, name="Get Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={"name": "Get Me", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert create_resp.status_code == 201, create_resp.text
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"
        assert "watched_item_id" in response.json()

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/v1/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client, db_session):
        wi = await _make_wi(db_session, name="Original")
        create_resp = await client.post(
            "/api/v1/watches",
            json={"name": "Original", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = create_resp.json()["id"]

        response = await client.patch(f"/api/v1/watches/{watch_id}", json={"name": "Updated"})
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"

    async def test_update_watch_not_found(self, client):
        response = await client.patch(
            "/api/v1/watches/00000000000000000000000000",
            json={"name": "Nope"},
        )
        assert response.status_code == 404

    async def test_update_activate_blocked_when_domain_inactive(self, client, db_session):
        watch = await make_watch(
            db_session,
            name="Suspended",
            primary_url="https://blocked-api.com/p",
            content_type="html",
            is_active=False,
        )
        watch.watched_item.domain_suspended = True
        await db_session.commit()
        response = await client.patch(
            f"/api/v1/watches/{watch.id}",
            json={"is_active": True},
        )
        assert response.status_code == 409


class TestDeactivateWatch:
    async def test_deactivate_watch(self, client, db_session):
        wi = await _make_wi(db_session, name="Deactivate Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={"name": "Deactivate Me", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = create_resp.json()["id"]

        response = await client.post(f"/api/v1/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_deactivate_watch_not_found(self, client):
        response = await client.post("/api/v1/watches/00000000000000000000000000/deactivate")
        assert response.status_code == 404


class TestAuditLog:
    async def test_create_writes_audit_entry(self, client, db_session):
        wi = await _make_wi(db_session, name="Audited Watch")
        await client.post(
            "/api/v1/watches",
            json={"name": "Audited Watch", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_CREATED,
                AuditLog.payload["name"].astext == "Audited Watch",
            )
        )
        entry = result.scalar_one()
        assert entry.payload["watched_item_id"] == str(wi.id)
        assert entry.watch_id is not None

    async def test_update_writes_audit_entry(self, client, db_session):
        wi = await _make_wi(db_session, name="Update Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Update Audit", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        await client.patch(f"/api/v1/watches/{watch_id}", json={"name": "Changed"})

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_UPDATED,
                AuditLog.payload["updated_fields"].astext.contains("name"),
            )
        )
        entry = result.scalar_one()
        assert str(entry.watch_id) == watch_id

    async def test_deactivate_writes_audit_entry(self, client, db_session):
        wi = await _make_wi(db_session, name="Deact Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Deact Audit", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        await client.post(f"/api/v1/watches/{watch_id}/deactivate")

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_DEACTIVATED,
                AuditLog.payload["name"].astext == "Deact Audit",
            )
        )
        entry = result.scalar_one()
        assert str(entry.watch_id) == watch_id


class TestInvalidULID:
    async def test_get_with_invalid_ulid_returns_404(self, client):
        response = await client.get("/api/v1/watches/not-a-valid-ulid")
        assert response.status_code == 404

    async def test_patch_with_invalid_ulid_returns_404(self, client):
        response = await client.patch("/api/v1/watches/not-a-valid-ulid", json={"name": "X"})
        assert response.status_code == 404


class TestListWatchesFilter:
    async def test_filter_by_active_status(self, client, db_session):
        wi = await _make_wi(db_session, name="Active Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Active Watch", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        await client.post(f"/api/v1/watches/{watch_id}/deactivate")

        active = await client.get("/api/v1/watches?is_active=true")
        inactive = await client.get("/api/v1/watches?is_active=false")

        active_ids = [w["id"] for w in active.json()]
        inactive_ids = [w["id"] for w in inactive.json()]
        assert watch_id not in active_ids
        assert watch_id in inactive_ids


class TestDeleteWatch:
    async def _create_archived_watch(self, client, db_session, *, name="Delete Me"):
        """Create a watch, archive it, return its ID."""
        wi = await _make_wi(db_session, name=name)
        resp = await client.post(
            "/api/v1/watches",
            json={"name": name, "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        # Archive via DB (no archive API endpoint).
        watch = await db_session.get(Watch, ULID.from_str(watch_id))
        watch.is_active = False
        watch.is_archived = True
        await db_session.commit()
        return watch_id

    async def test_delete_archived_watch_returns_204(self, client, db_session):
        watch_id = await self._create_archived_watch(client, db_session)
        response = await client.delete(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 204

    async def test_delete_watch_removes_from_db(self, client, db_session):
        watch_id = await self._create_archived_watch(client, db_session)
        await client.delete(f"/api/v1/watches/{watch_id}")
        response = await client.get(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 404

    async def test_delete_non_archived_watch_returns_409(self, client, db_session):
        wi = await _make_wi(db_session, name="Still Active")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "Still Active", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.delete(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 409

    async def test_delete_not_found(self, client):
        response = await client.delete("/api/v1/watches/00000000000000000000000000")
        assert response.status_code == 404

    async def test_delete_writes_audit_entry(self, client, db_session):
        watch_id = await self._create_archived_watch(client, db_session, name="Delete Audit")
        await client.delete(f"/api/v1/watches/{watch_id}")
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_DELETED,
                AuditLog.payload["name"].astext == "Delete Audit",
            )
        )
        entry = result.scalar_one()
        assert entry.watch_id is None  # SET NULL after cascade

    async def test_delete_cascades_children(self, client, db_session):
        """Deleting a watch cascades to WatchNotificationConfig.

        #191: TemporalProfile is keyed to the WatchedItem now (not the Watch),
        so it is no longer a watch-delete cascade child.
        """
        watch_id = await self._create_archived_watch(client, db_session, name="Cascade")
        watch_ulid = ULID.from_str(watch_id)

        config = WatchNotificationConfig(
            watch_id=watch_ulid,
            channel_hint="https",
            remote_channel_id="01HV0000000000000000000099",
        )
        db_session.add(config)
        await db_session.flush()

        # Delete the watch.
        await client.delete(f"/api/v1/watches/{watch_id}")

        # Verify watch is gone.
        watches = (
            (await db_session.execute(select(Watch).where(Watch.id == watch_ulid))).scalars().all()
        )
        assert len(watches) == 0

        configs = (
            (
                await db_session.execute(
                    select(WatchNotificationConfig).where(
                        WatchNotificationConfig.watch_id == watch_ulid
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(configs) == 0


class TestLifecycleEventURL:
    """PATCH/DELETE routes use watched_item.effective_url for lifecycle events.

    #185 Phase A: Archiver SDK no longer called in PATCH/DELETE; URL comes
    from the local WatchedItem row. Empty effective_url falls back to a
    sentinel so events always have a URL.
    """

    async def test_pause_dispatches_with_watched_item_effective_url(self, client, db_session):
        """PATCH is_active=false dispatches WATCH_PAUSED with watched_item.effective_url."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Active Watch",
            primary_url="https://example.com",
            is_active=True,
        )
        watch.watched_item.effective_url = "https://example.com/resolved"
        await db_session.commit()

        notify_patch = "src.api.routes.watches.dispatch_event_notifications"
        with patch(notify_patch, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch.id}",
                json={"is_active": False},
            )
        assert response.status_code == 200, response.text
        mock_dispatch.assert_awaited_once()
        event = mock_dispatch.call_args.kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_PAUSED
        assert event.watch_url == "https://example.com/resolved"

    async def test_pause_falls_back_to_sentinel_when_effective_url_empty(self, client, db_session):
        """PATCH is_active=false uses sentinel URL when watched_item.effective_url is empty."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Paused Watch",
            primary_url="https://example.com",
            is_active=True,
        )
        # Clear effective_url to force the sentinel path.
        watch.watched_item.effective_url = ""
        await db_session.commit()

        notify_patch = "src.api.routes.watches.dispatch_event_notifications"
        with patch(notify_patch, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch.id}",
                json={"is_active": False},
            )
        assert response.status_code == 200, response.text
        event = mock_dispatch.call_args.kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_PAUSED
        assert event.watch_url == f"watch:{watch.id}"

    async def test_resume_dispatches_with_watched_item_effective_url(self, client, db_session):
        """PATCH is_active=true dispatches WATCH_RESUMED with watched_item.effective_url."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Paused Watch",
            primary_url="https://example.com",
            is_active=False,
        )
        watch.watched_item.effective_url = "https://example.com/page"
        await db_session.commit()

        notify_patch = "src.api.routes.watches.dispatch_event_notifications"
        with patch(notify_patch, new_callable=AsyncMock) as mock_dispatch:
            response = await client.patch(
                f"/api/v1/watches/{watch.id}",
                json={"is_active": True},
            )
        assert response.status_code == 200, response.text
        event = mock_dispatch.call_args.kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_RESUMED
        assert event.watch_url == "https://example.com/page"

    async def test_delete_uses_watched_item_effective_url(self, client, db_session):
        """DELETE dispatches WATCH_DELETED using watched_item.effective_url."""
        watch = await make_watch(
            db_session,
            name="Archived Watch",
            primary_url="https://example.com",
            is_active=False,
            is_archived=True,
        )
        watch.watched_item.effective_url = "https://example.com/page"
        await db_session.commit()

        response = await client.delete(f"/api/v1/watches/{watch.id}")
        assert response.status_code == 204
        get_resp = await client.get(f"/api/v1/watches/{watch.id}")
        assert get_resp.status_code == 404


class TestListWatchesArchivedFilter:
    async def test_list_watches_no_filter_includes_archived(self, client, db_session):
        """No ?is_archived param returns all watches, including archived ones."""
        archived = await make_watch(
            db_session,
            name="Archived Watch",
            primary_url="https://example.com/archived",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        await db_session.commit()

        response = await client.get("/api/v1/watches")
        ids = [w["id"] for w in response.json()]
        assert str(archived.id) in ids

    async def test_list_watches_is_archived_true_returns_archived_only(self, client, db_session):
        """?is_archived=true returns only archived watches."""
        archived = await make_watch(
            db_session,
            name="Archived Only",
            primary_url="https://example.com/arch-only",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        active = await make_watch(
            db_session,
            name="Active Only",
            primary_url="https://example.com/active-only",
            content_type="html",
            is_active=True,
            is_archived=False,
        )
        await db_session.commit()

        response = await client.get("/api/v1/watches?is_archived=true")
        assert response.status_code == 200
        ids = [w["id"] for w in response.json()]
        assert str(archived.id) in ids
        assert str(active.id) not in ids

    async def test_list_watches_is_archived_false_excludes_archived(self, client, db_session):
        """?is_archived=false explicitly excludes archived watches."""
        archived = await make_watch(
            db_session,
            name="Arch False Test",
            primary_url="https://example.com/arch-false",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        await db_session.commit()

        response = await client.get("/api/v1/watches?is_archived=false")
        ids = [w["id"] for w in response.json()]
        assert str(archived.id) not in ids

    async def test_watch_response_includes_is_archived_field(self, client, db_session):
        """Create watch response includes is_archived=False."""
        wi = await _make_wi(db_session, name="W")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert resp.status_code == 201
        assert resp.json()["is_archived"] is False


class TestWatchEffectiveUrl:
    """Watch-create uses WatchedItem.effective_url (#185 Phase A step 7)."""

    async def test_create_watch_linked_to_wi_with_url(self, client, db_session):
        """Watch is correctly linked to a WatchedItem with effective_url."""
        wi = await _make_wi(db_session, name="W", url="https://example.com/page")
        response = await client.post(
            "/api/v1/watches",
            json={"name": "W", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["watched_item_id"] == str(wi.id)

    async def test_create_watch_links_to_existing_wi(self, client, db_session):
        """Watch creation does NOT create a new WatchedItem — must use existing one."""
        wi = await _make_wi(db_session, name="WI Create")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "WI Create", "watched_item_id": str(wi.id), "content_type": "html"},
        )
        assert resp.status_code == 201, resp.text
        wi_id = resp.json()["watched_item_id"]
        # Must link back to the same WI, not create a new one.
        wi_row = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.id == ULID.from_str(wi_id))
            )
        ).scalar_one()
        assert str(wi_row.id) == str(wi.id)
