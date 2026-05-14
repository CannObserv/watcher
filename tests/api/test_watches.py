"""Integration tests for Watch CRUD API endpoints.

Phase 5 contract: ``POST /api/v1/watches`` takes ``{name, info_source_id,
content_type}``. The route resolves the URL via the ArchiverClient SDK
at create-time. The Watch row stores info_source_id (NOT NULL).
WatchResponse exposes info_source_id.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from archiver_client import AuthError, NotFound, ServerError
from sqlalchemy import select
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEventType
from tests.conftest import make_info_item, make_info_source, make_watch

pytestmark = pytest.mark.integration


async def _seed_info_item(db_session, *, name="Test InfoItem", url="https://example.com/page"):
    """Create an InfoItem + InfoSource; return (info_item_id, info_source_id) as str tuple.

    Phase 5: info_specs table is gone. The conftest info_client mock returns a
    synthesised spec stub for any info_item_id, so no real InfoSpec row is needed.
    """
    item = await make_info_item(db_session, name=name)
    source = await make_info_source(db_session, url=url)
    await db_session.commit()
    return str(item.info_item_id), str(source.info_source_id)


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Test Watch",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["info_source_id"] == info_source_id
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data
        # No legacy fields on the new shape.
        assert "url" not in data
        assert "fetch_config" not in data

    async def test_create_watch_with_schedule_config(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, url="https://example.com/report.pdf"
        )
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "PDF Watch",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "pdf",
                "schedule_config": {"interval": "6h"},
            },
        )
        assert response.status_code == 201, response.text

    async def test_create_watch_invalid_content_type(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "invalid",
            },
        )
        assert response.status_code == 422

    async def test_create_watch_missing_info_source_id_returns_422(self, client):
        response = await client.post(
            "/api/v1/watches",
            json={"name": "No source", "content_type": "html"},
        )
        assert response.status_code == 422

    async def test_create_watch_unknown_info_source_id_returns_422(self, client, info_client):
        info_client.get_info_source.side_effect = NotFound("not found")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_source_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 422
        assert "info_source_id" in response.text

    async def test_create_watch_sdk_connection_error_returns_503(self, client, info_client):
        info_client.get_info_source.side_effect = httpx.ConnectError("unreachable")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_source_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"

    async def test_create_watch_sdk_auth_error_returns_500(self, client, info_client):
        info_client.get_info_source.side_effect = AuthError("forbidden")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_source_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 500


class TestListWatches:
    async def test_list_watches_empty(self, client):
        response = await client.get("/api/v1/watches")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_watches_returns_created(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Watch 1")
        await client.post(
            "/api/v1/watches",
            json={
                "name": "Watch 1",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        response = await client.get("/api/v1/watches")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Watch 1"


class TestGetWatch:
    async def test_get_watch_by_id(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Get Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Get Me",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"
        assert response.json()["info_source_id"] == info_source_id

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/v1/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Original")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Original",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = create_resp.json()["id"]

        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"name": "Updated"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"

    async def test_update_watch_not_found(self, client):
        response = await client.patch(
            "/api/v1/watches/00000000000000000000000000",
            json={"name": "Nope"},
        )
        assert response.status_code == 404

    async def test_update_activate_blocked_when_domain_inactive(self, client, db_session):
        db_session.add(Domain(name="blocked-api.com", is_active=False))
        watch = await make_watch(
            db_session,
            name="Suspended",
            url="https://blocked-api.com/p",
            content_type="html",
            effective_domain="blocked-api.com",
            is_active=False,
        )
        await db_session.commit()
        response = await client.patch(
            f"/api/v1/watches/{watch.id}",
            json={"is_active": True},
        )
        assert response.status_code == 409


class TestDeactivateWatch:
    async def test_deactivate_watch(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Deactivate Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Deactivate Me",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Audited Watch")
        await client.post(
            "/api/v1/watches",
            json={
                "name": "Audited Watch",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_CREATED,
                AuditLog.payload["name"].astext == "Audited Watch",
            )
        )
        entry = result.scalar_one()
        assert entry.payload["info_source_id"] == info_source_id
        assert entry.watch_id is not None

    async def test_update_writes_audit_entry(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Update Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Update Audit",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Deact Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Deact Audit",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Active Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Active Watch",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name=name)
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": name,
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        # Archive via DB (no archive API endpoint)
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name="Still Active")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Still Active",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
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
        """Deleting a watch cascades to all child records.

        Phase 5 (#156): Snapshot/SnapshotChunk tables dropped — only TemporalProfile
        and WatchNotificationConfig cascade is verified here.
        """
        watch_id = await self._create_archived_watch(client, db_session, name="Cascade")
        watch_ulid = ULID.from_str(watch_id)

        profile = TemporalProfile(
            watch_id=watch_ulid,
            profile_type="event",
            post_action="deactivate",
        )
        config = WatchNotificationConfig(
            watch_id=watch_ulid,
            channel_hint="https",
            remote_channel_id="01HV0000000000000000000099",
        )
        db_session.add_all([profile, config])
        await db_session.flush()

        # Delete the watch
        await client.delete(f"/api/v1/watches/{watch_id}")

        # Verify watch is gone
        watches = (
            (await db_session.execute(select(Watch).where(Watch.id == watch_ulid))).scalars().all()
        )
        assert len(watches) == 0

        profiles = (
            (
                await db_session.execute(
                    select(TemporalProfile).where(TemporalProfile.watch_id == watch_ulid)
                )
            )
            .scalars()
            .all()
        )
        assert len(profiles) == 0

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


class TestSDKFailureHandling:
    """SDK-failure paths in PATCH/DELETE handlers must not 5xx the user request.

    Pattern mirrors ``src/workers/tasks.py``: catch ``NotFound`` from the SDK
    when resolving ``watch_url`` and degrade gracefully (sentinel URL or skip
    the notification dispatch) so an orphaned InfoSpec doesn't block operator
    actions on the watch row.
    """

    async def test_pause_dispatches_event_when_resolve_url_raises_notfound(
        self, client, db_session
    ):
        """PATCH ``is_active=false`` must still dispatch a WATCH_PAUSED event with a
        sentinel URL when the SDK raises NotFound (orphaned InfoItem)."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Active Watch",
            url="https://example.com",
            is_active=True,
        )
        await db_session.commit()

        notify_patch = "src.api.routes.watches.dispatch_event_notifications"
        with (
            patch(
                "src.api.routes.watches.resolve_watch_url",
                new_callable=AsyncMock,
                side_effect=NotFound("info_item missing"),
            ),
            patch(notify_patch, new_callable=AsyncMock) as mock_dispatch,
        ):
            response = await client.patch(
                f"/api/v1/watches/{watch.id}",
                json={"is_active": False},
            )
        assert response.status_code == 200, response.text
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_PAUSED
        assert event.watch_url == f"watch:{watch.id}"

    async def test_resume_dispatches_event_when_resolve_url_raises_notfound(
        self, client, db_session
    ):
        """PATCH ``is_active=true`` must still dispatch WATCH_RESUMED with a sentinel URL."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Paused Watch",
            url="https://example.com",
            is_active=False,
        )
        await db_session.commit()

        notify_patch = "src.api.routes.watches.dispatch_event_notifications"
        with (
            patch(
                "src.api.routes.watches.resolve_watch_url",
                new_callable=AsyncMock,
                side_effect=NotFound("info_item missing"),
            ),
            patch(notify_patch, new_callable=AsyncMock) as mock_dispatch,
        ):
            response = await client.patch(
                f"/api/v1/watches/{watch.id}",
                json={"is_active": True},
            )
        assert response.status_code == 200, response.text
        mock_dispatch.assert_awaited_once()
        _, kwargs = mock_dispatch.call_args
        event = kwargs["event"]
        assert event.event_type == WatchEventType.WATCH_RESUMED
        assert event.watch_url == f"watch:{watch.id}"

    async def test_delete_completes_when_resolve_url_raises_notfound(self, client, db_session):
        """DELETE on archived watch must succeed even if InfoItem is gone."""
        from unittest.mock import patch

        watch = await make_watch(
            db_session,
            name="Archived Watch",
            url="https://example.com",
            is_active=False,
            is_archived=True,
        )
        await db_session.commit()

        with patch(
            "src.api.routes.watches.resolve_watch_url",
            new_callable=AsyncMock,
            side_effect=NotFound("info_item missing"),
        ):
            response = await client.delete(f"/api/v1/watches/{watch.id}")
        assert response.status_code == 204, (
            f"DELETE must complete despite NotFound; got {response.status_code} {response.text}"
        )
        # Watch must actually be gone
        get_resp = await client.get(f"/api/v1/watches/{watch.id}")
        assert get_resp.status_code == 404


class TestListWatchesArchivedFilter:
    async def test_list_watches_no_filter_includes_archived(self, client, db_session):
        """No ?is_archived param returns all watches, including archived ones."""
        archived = await make_watch(
            db_session,
            name="Archived Watch",
            url="https://example.com/archived",
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
            url="https://example.com/arch-only",
            content_type="html",
            is_active=False,
            is_archived=True,
        )
        active = await make_watch(
            db_session,
            name="Active Only",
            url="https://example.com/active-only",
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
            url="https://example.com/arch-false",
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
        info_item_id, info_source_id = await _seed_info_item(db_session, name="W")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["is_archived"] is False


class TestCreateWatchProbe:
    async def test_create_watch_populates_effective_fields(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/page"
        )
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"

    async def test_create_watch_upserts_domain(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        domains = (await client.get("/api/v1/domains")).json()
        assert any(d["name"] == "example.com" for d in domains)

    async def test_create_watch_does_not_overwrite_existing_domain_config(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        await client.patch("/api/v1/domains/example.com", json={"min_interval": 10.0})
        await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        domain = (await client.get("/api/v1/domains/example.com")).json()
        assert domain["min_interval"] == 10.0  # operator config preserved

    async def test_create_watch_sdk_server_error_returns_503(self, client, info_client):
        """ServerError from the SDK during create maps to 503 with Retry-After."""
        info_client.get_info_source.side_effect = ServerError("boom", status_code=500)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "info_source_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"


class TestUpdateWatchEffectiveFields:
    async def test_patch_effective_url(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_url": "https://example.com/resolved"},
        )
        assert response.status_code == 200
        assert response.json()["effective_url"] == "https://example.com/resolved"

    async def test_patch_effective_domain(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": "cdn.example.com"},
        )
        assert response.status_code == 200
        assert response.json()["effective_domain"] == "cdn.example.com"

    async def test_patch_effective_url_null(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_url": None},
        )
        assert response.status_code == 200
        assert response.json()["effective_url"] is None

    async def test_patch_effective_domain_null(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": None},
        )
        assert response.status_code == 200
        assert response.json()["effective_domain"] is None

    async def test_patch_effective_domain_too_long_returns_422(self, client, db_session):
        info_item_id, info_source_id = await _seed_info_item(
            db_session, name="W", url="https://example.com/p"
        )
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "W",
                "info_item_id": info_item_id,
                "info_source_id": info_source_id,
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": "a" * 254},
        )
        assert response.status_code == 422


class TestFragmentRootInvariants:
    """Fragment-root invariants on Watch create/delete (Task 5.3)."""

    ROOT_ID = "01HZZ00000000000000000ROOT"
    FRAG_ID = "01HZZ000000000000000FRAGMT"

    def _stub_fragment_chain(self, info_client):
        """Make get_info_source report FRAG_ID as a child of ROOT_ID."""
        info_client.get_info_source = AsyncMock(
            side_effect=[
                MagicMock(
                    info_source_id=self.FRAG_ID,
                    parent_info_source_id=self.ROOT_ID,
                ),
                MagicMock(
                    info_source_id=self.ROOT_ID,
                    parent_info_source_id=None,
                ),
            ]
        )

    def _stub_no_fragment_dependents(self, info_client):
        """Make list_info_sources return empty (no fragments under root)."""
        info_client.list_info_sources = AsyncMock(return_value=MagicMock(items=[]))

    def _stub_fragment_dependents(self, info_client, frag_source_id):
        """Make list_info_sources return one fragment child under root."""
        info_client.list_info_sources = AsyncMock(
            return_value=MagicMock(
                items=[
                    MagicMock(info_source_id=frag_source_id),
                ]
            )
        )

    async def test_create_fragment_watch_rejects_without_root(
        self, client, db_session, info_client
    ):
        """POST with a fragment info_source_id and no root Watch → 422 domain error.

        The invariant check fires before _create_watch, so no InfoSpec row is
        needed — we stub get_info_source to report FRAG_ID as a fragment and
        the response must be 422 without reaching the URL-resolution phase.
        """
        # Create InfoItem only (no InfoSpec) — the 422 fires before resolve_primary.
        frag_info_item = await make_info_item(db_session, name="Frag Item 422")
        info_item_id = str(frag_info_item.info_item_id)
        await db_session.commit()

        # Stub: FRAG_ID is a child of ROOT_ID; no Watch exists for either.
        self._stub_fragment_chain(info_client)

        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Frag Watch",
                "info_item_id": info_item_id,
                "info_source_id": self.FRAG_ID,
                "content_type": "html",
            },
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["kind"] == "domain"
        assert "root" in detail["message"].lower()

    async def test_create_fragment_watch_accepts_when_root_watched(
        self, client, db_session, info_client
    ):
        """POST with a fragment info_source_id succeeds when an active root Watch exists.

        Uses real InfoSource rows for both root and fragment (so FK constraints
        pass) and stubs get_primary_info_spec to bypass the missing
        information.info_specs table in the test DB.
        """
        # Create real InfoSource rows — required for the FK on watches.info_source_id.
        root_src = await make_info_source(db_session)
        frag_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
        root_info_item = await make_info_item(db_session, name="Root Item OK")
        await make_watch(
            db_session,
            name="Root Watch OK",
            info_item_id=root_info_item.info_item_id,
            info_source_id=root_src.info_source_id,
            is_active=True,
            is_archived=False,
        )
        # Create InfoItem for the fragment watch (no InfoSpec needed — we stub).
        frag_info_item = await make_info_item(db_session, name="Frag Item OK")
        await db_session.commit()

        # Stub: frag_src is child of root_src (which has an active Watch).
        # Four calls total:
        #   (1) route initial fragment check → frag_src (has parent)
        #   (2) _walk_to_root iteration → frag_src (has parent)
        #   (3) _walk_to_root iteration → root_src (no parent, root Watch found)
        #   (4) create_watch URL resolution → frag_src with source_spec
        frag_source_mock = MagicMock(
            info_source_id=str(frag_src.info_source_id),
            parent_info_source_id=str(root_src.info_source_id),
        )
        frag_source_mock.source_spec.additional_properties = {
            "target": {"url": "https://example.com/frag-ok"}
        }
        info_client.get_info_source = AsyncMock(
            side_effect=[
                MagicMock(
                    info_source_id=str(frag_src.info_source_id),
                    parent_info_source_id=str(root_src.info_source_id),
                ),
                MagicMock(
                    info_source_id=str(frag_src.info_source_id),
                    parent_info_source_id=str(root_src.info_source_id),
                ),
                MagicMock(
                    info_source_id=str(root_src.info_source_id),
                    parent_info_source_id=None,
                ),
                frag_source_mock,  # 4th call: create_watch URL resolution
            ]
        )

        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Frag Watch OK",
                "info_item_id": str(frag_info_item.info_item_id),
                "info_source_id": str(frag_src.info_source_id),
                "content_type": "html",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["name"] == "Frag Watch OK"

    async def test_delete_root_watch_blocks_when_fragments_exist(
        self, client, db_session, info_client
    ):
        """DELETE on a root Watch with active fragment Watches → 409 conflict."""
        # Create root source + watch.
        root_src = await make_info_source(db_session)
        root_item = await make_info_item(db_session, name="Root Delete")
        root_watch = await make_watch(
            db_session,
            name="Root Delete Watch",
            info_item_id=root_item.info_item_id,
            info_source_id=root_src.info_source_id,
            is_active=False,
            is_archived=True,
        )
        # Create fragment source + watch.
        frag_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
        frag_item = await make_info_item(db_session, name="Frag Delete")
        frag_watch = await make_watch(
            db_session,
            name="Frag Delete Watch",
            info_item_id=frag_item.info_item_id,
            info_source_id=frag_src.info_source_id,
            is_active=True,
            is_archived=False,
        )
        await db_session.commit()

        # Stub: root_src has no parent (it's a root); list_info_sources returns frag_src.
        info_client.get_info_source = AsyncMock(
            return_value=MagicMock(
                info_source_id=str(root_src.info_source_id),
                parent_info_source_id=None,
            )
        )
        self._stub_fragment_dependents(info_client, frag_src.info_source_id)

        response = await client.delete(f"/api/v1/watches/{root_watch.id}")
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["kind"] == "conflict"
        dependents = detail["data"]["dependents"]
        assert len(dependents) >= 1
        assert str(frag_watch.id) in [d["watch_id"] for d in dependents]

    async def test_delete_root_watch_cascade_archives_fragments(
        self, client, db_session, info_client
    ):
        """DELETE ?cascade=true archives fragment Watches and proceeds with root deletion."""
        # Create root source + watch (archived so delete is allowed).
        root_src = await make_info_source(db_session)
        root_item = await make_info_item(db_session, name="Root Cascade")
        root_watch = await make_watch(
            db_session,
            name="Root Cascade Watch",
            info_item_id=root_item.info_item_id,
            info_source_id=root_src.info_source_id,
            is_active=False,
            is_archived=True,
        )
        # Create fragment source + watch (active).
        frag_src = await make_info_source(db_session, parent_info_source_id=root_src.info_source_id)
        frag_item = await make_info_item(db_session, name="Frag Cascade")
        frag_watch = await make_watch(
            db_session,
            name="Frag Cascade Watch",
            info_item_id=frag_item.info_item_id,
            info_source_id=frag_src.info_source_id,
            is_active=True,
            is_archived=False,
        )
        await db_session.commit()

        # Stub: root_src has no parent; list_info_sources returns frag_src.
        # The MagicMock must also support source_spec.additional_properties["target"]["url"]
        # for resolve_watch_url (called during delete notification dispatch).
        root_source_mock = MagicMock(
            info_source_id=str(root_src.info_source_id),
            parent_info_source_id=None,
        )
        root_source_mock.source_spec.additional_properties = {
            "target": {"url": "https://example.com/cascade"}
        }
        info_client.get_info_source = AsyncMock(return_value=root_source_mock)
        self._stub_fragment_dependents(info_client, frag_src.info_source_id)

        response = await client.delete(f"/api/v1/watches/{root_watch.id}?cascade=true")
        assert response.status_code == 204, response.text

        # Fragment watch must be archived and inactive.
        await db_session.refresh(frag_watch)
        assert frag_watch.is_archived is True
        assert frag_watch.is_active is False

        # Root watch must be gone.
        get_resp = await client.get(f"/api/v1/watches/{root_watch.id}")
        assert get_resp.status_code == 404
