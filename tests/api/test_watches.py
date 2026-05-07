"""Integration tests for Watch CRUD API endpoints.

Phase 2c contract: ``POST /api/v1/watches`` takes ``{name, info_item_id,
content_type}`` (no ``url`` / ``fetch_config``). The route resolves the
URL via the SDK at create-time so ``effective_url`` / ``effective_domain``
remain populated for downstream rate-limiting / domain upsert.

``WatchResponse`` exposes neither ``url`` nor ``fetch_config`` — those
columns no longer exist on the model. Tests assert the new shape directly.
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from archiver_client import AuthError, NotFound, ServerError
from sqlalchemy import select
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch
from src.core.notifications.events import WatchEventType
from tests.conftest import make_info_item, make_info_spec, make_watch

pytestmark = pytest.mark.integration


async def _seed_info_item(db_session, *, name="Test InfoItem", url="https://example.com/page"):
    """Create an InfoItem + primary InfoSpec; return its id (str)."""
    item = await make_info_item(db_session, name=name)
    await make_info_spec(db_session, item, url=url)
    await db_session.commit()
    return str(item.info_item_id)


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client, db_session):
        info_item_id = await _seed_info_item(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Test Watch",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["info_item_id"] == info_item_id
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data
        # No legacy fields on the new shape.
        assert "url" not in data
        assert "fetch_config" not in data

    async def test_create_watch_with_schedule_config(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, url="https://example.com/report.pdf")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "PDF Watch",
                "info_item_id": info_item_id,
                "content_type": "pdf",
                "schedule_config": {"interval": "6h"},
            },
        )
        assert response.status_code == 201, response.text

    async def test_create_watch_invalid_content_type(self, client, db_session):
        info_item_id = await _seed_info_item(db_session)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": info_item_id,
                "content_type": "invalid",
            },
        )
        assert response.status_code == 422

    async def test_create_watch_missing_info_item_id_returns_422(self, client):
        response = await client.post(
            "/api/v1/watches",
            json={"name": "No item", "content_type": "html"},
        )
        assert response.status_code == 422

    async def test_create_watch_unknown_info_item_id_returns_422(self, client, info_client):
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 422
        assert "info_item_id" in response.text

    async def test_create_watch_sdk_connection_error_returns_503(self, client, info_client):
        info_client.get_primary_info_spec.side_effect = httpx.ConnectError("unreachable")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"

    async def test_create_watch_sdk_auth_error_returns_500(self, client, info_client):
        info_client.get_primary_info_spec.side_effect = AuthError("forbidden")
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
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
        info_item_id = await _seed_info_item(db_session, name="Watch 1")
        await client.post(
            "/api/v1/watches",
            json={
                "name": "Watch 1",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Get Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Get Me",
                "info_item_id": info_item_id,
                "content_type": "html",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/v1/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"
        assert response.json()["info_item_id"] == info_item_id

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/v1/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Original")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Original",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Deactivate Me")
        create_resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Deactivate Me",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Audited Watch")
        await client.post(
            "/api/v1/watches",
            json={
                "name": "Audited Watch",
                "info_item_id": info_item_id,
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
        assert entry.payload["info_item_id"] == info_item_id
        assert entry.watch_id is not None

    async def test_update_writes_audit_entry(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="Update Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Update Audit",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Deact Audit")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Deact Audit",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Active Watch")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Active Watch",
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name=name)
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": name,
                "info_item_id": info_item_id,
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
        info_item_id = await _seed_info_item(db_session, name="Still Active")
        resp = await client.post(
            "/api/v1/watches",
            json={
                "name": "Still Active",
                "info_item_id": info_item_id,
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
        """Deleting a watch cascades to all child records."""
        watch_id = await self._create_archived_watch(client, db_session, name="Cascade")

        # Insert child records directly via session
        watch_ulid = ULID.from_str(watch_id)

        snapshot = Snapshot(
            watch_id=watch_ulid,
            content_hash="a" * 64,
            simhash=123,
            storage_path="/tmp/test",
            text_path="/tmp/test.txt",
            chunk_count=1,
            text_bytes=100,
            fetch_duration_ms=50,
            fetcher_used="http",
        )
        db_session.add(snapshot)
        await db_session.flush()

        chunk = SnapshotChunk(
            snapshot_id=snapshot.id,
            chunk_index=0,
            chunk_type="section",
            chunk_label="test",
            content_hash="b" * 64,
            simhash=456,
            char_count=50,
            excerpt="test content",
        )
        profile = TemporalProfile(
            watch_id=watch_ulid,
            profile_type="event",
            post_action="deactivate",
        )
        config = WatchNotificationConfig(
            watch_id=watch_ulid,
            apprise_url="https://hooks.example.com/abc",
            channel_hint="https",
        )
        db_session.add_all([chunk, profile, config])
        await db_session.flush()

        # Delete the watch
        await client.delete(f"/api/v1/watches/{watch_id}")

        # Verify children are gone
        watches = (
            (await db_session.execute(select(Watch).where(Watch.id == watch_ulid))).scalars().all()
        )
        assert len(watches) == 0

        snapshots = (
            (await db_session.execute(select(Snapshot).where(Snapshot.watch_id == watch_ulid)))
            .scalars()
            .all()
        )
        assert len(snapshots) == 0

        chunks = (
            (
                await db_session.execute(
                    select(SnapshotChunk).where(SnapshotChunk.snapshot_id == snapshot.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(chunks) == 0

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
        info_item_id = await _seed_info_item(db_session, name="W")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        assert resp.status_code == 201
        assert resp.json()["is_archived"] is False


class TestCreateWatchProbe:
    async def test_create_watch_populates_effective_fields(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/page")
        response = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["effective_url"] == "https://example.com/page"
        assert data["effective_domain"] == "example.com"

    async def test_create_watch_upserts_domain(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        domains = (await client.get("/api/v1/domains")).json()
        assert any(d["name"] == "example.com" for d in domains)

    async def test_create_watch_does_not_overwrite_existing_domain_config(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        await client.patch("/api/v1/domains/example.com", json={"min_interval": 10.0})
        await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        domain = (await client.get("/api/v1/domains/example.com")).json()
        assert domain["min_interval"] == 10.0  # operator config preserved

    async def test_create_watch_sdk_server_error_returns_503(self, client, info_client):
        """ServerError from the SDK during create maps to 503 with Retry-After."""
        info_client.get_primary_info_spec.side_effect = ServerError("boom", status_code=500)
        response = await client.post(
            "/api/v1/watches",
            json={
                "name": "Bad",
                "info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                "content_type": "html",
            },
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"


class TestUpdateWatchEffectiveFields:
    async def test_patch_effective_url(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_url": "https://example.com/resolved"},
        )
        assert response.status_code == 200
        assert response.json()["effective_url"] == "https://example.com/resolved"

    async def test_patch_effective_domain(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": "cdn.example.com"},
        )
        assert response.status_code == 200
        assert response.json()["effective_domain"] == "cdn.example.com"

    async def test_patch_effective_url_null(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_url": None},
        )
        assert response.status_code == 200
        assert response.json()["effective_url"] is None

    async def test_patch_effective_domain_null(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": None},
        )
        assert response.status_code == 200
        assert response.json()["effective_domain"] is None

    async def test_patch_effective_domain_too_long_returns_422(self, client, db_session):
        info_item_id = await _seed_info_item(db_session, name="W", url="https://example.com/p")
        resp = await client.post(
            "/api/v1/watches",
            json={"name": "W", "info_item_id": info_item_id, "content_type": "html"},
        )
        watch_id = resp.json()["id"]
        response = await client.patch(
            f"/api/v1/watches/{watch_id}",
            json={"effective_domain": "a" * 254},
        )
        assert response.status_code == 422
