"""Integration tests for Watch CRUD API endpoints."""

import pytest
from sqlalchemy import select
from ulid import ULID

from src.core.models.audit_log import AuditLog
from src.core.models.notification_config import NotificationConfig
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch

pytestmark = pytest.mark.integration


class TestCreateWatch:
    async def test_create_watch_returns_201(self, client):
        response = await client.post(
            "/api/watches",
            json={
                "name": "Test Watch",
                "url": "https://example.com/page",
                "content_type": "html",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Watch"
        assert data["url"] == "https://example.com/page"
        assert data["content_type"] == "html"
        assert data["is_active"] is True
        assert "id" in data
        assert "created_at" in data

    async def test_create_watch_with_config(self, client):
        response = await client.post(
            "/api/watches",
            json={
                "name": "PDF Watch",
                "url": "https://example.com/report.pdf",
                "content_type": "pdf",
                "fetch_config": {"timeout": 30},
                "schedule_config": {"interval": "6h"},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["fetch_config"] == {"timeout": 30}

    async def test_create_watch_invalid_content_type(self, client):
        response = await client.post(
            "/api/watches",
            json={
                "name": "Bad",
                "url": "https://example.com",
                "content_type": "invalid",
            },
        )
        assert response.status_code == 422


class TestListWatches:
    async def test_list_watches_empty(self, client):
        response = await client.get("/api/watches")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_watches_returns_created(self, client):
        await client.post(
            "/api/watches",
            json={
                "name": "Watch 1",
                "url": "https://example.com/1",
                "content_type": "html",
            },
        )
        response = await client.get("/api/watches")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Watch 1"


class TestGetWatch:
    async def test_get_watch_by_id(self, client):
        create_resp = await client.post(
            "/api/watches",
            json={
                "name": "Get Me",
                "url": "https://example.com/get",
                "content_type": "html",
            },
        )
        watch_id = create_resp.json()["id"]

        response = await client.get(f"/api/watches/{watch_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Get Me"

    async def test_get_watch_not_found(self, client):
        response = await client.get("/api/watches/00000000000000000000000000")
        assert response.status_code == 404


class TestUpdateWatch:
    async def test_update_watch_partial(self, client):
        create_resp = await client.post(
            "/api/watches",
            json={
                "name": "Original",
                "url": "https://example.com/orig",
                "content_type": "html",
            },
        )
        watch_id = create_resp.json()["id"]

        response = await client.patch(
            f"/api/watches/{watch_id}",
            json={
                "name": "Updated",
            },
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated"
        assert response.json()["url"] == "https://example.com/orig"

    async def test_update_watch_not_found(self, client):
        response = await client.patch(
            "/api/watches/00000000000000000000000000",
            json={"name": "Nope"},
        )
        assert response.status_code == 404


class TestDeactivateWatch:
    async def test_deactivate_watch(self, client):
        create_resp = await client.post(
            "/api/watches",
            json={
                "name": "Deactivate Me",
                "url": "https://example.com/deact",
                "content_type": "html",
            },
        )
        watch_id = create_resp.json()["id"]

        response = await client.post(f"/api/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_deactivate_watch_not_found(self, client):
        response = await client.post("/api/watches/00000000000000000000000000/deactivate")
        assert response.status_code == 404


class TestAuditLog:
    async def test_create_writes_audit_entry(self, client, db_session):
        await client.post(
            "/api/watches",
            json={
                "name": "Audited Watch",
                "url": "https://example.com/audit",
                "content_type": "html",
            },
        )
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "watch.created",
                AuditLog.payload["name"].astext == "Audited Watch",
            )
        )
        entry = result.scalar_one()
        assert entry.payload["url"] == "https://example.com/audit"
        assert entry.watch_id is not None

    async def test_update_writes_audit_entry(self, client, db_session):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Update Audit",
                "url": "https://example.com/upd",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.patch(f"/api/watches/{watch_id}", json={"name": "Changed"})

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "watch.updated",
                AuditLog.payload["updated_fields"].astext.contains("name"),
            )
        )
        entry = result.scalar_one()
        assert str(entry.watch_id) == watch_id

    async def test_deactivate_writes_audit_entry(self, client, db_session):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Deact Audit",
                "url": "https://example.com/deact-audit",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/api/watches/{watch_id}/deactivate")

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "watch.deactivated",
                AuditLog.payload["name"].astext == "Deact Audit",
            )
        )
        entry = result.scalar_one()
        assert str(entry.watch_id) == watch_id


class TestInvalidULID:
    async def test_get_with_invalid_ulid_returns_404(self, client):
        response = await client.get("/api/watches/not-a-valid-ulid")
        assert response.status_code == 404

    async def test_patch_with_invalid_ulid_returns_404(self, client):
        response = await client.patch("/api/watches/not-a-valid-ulid", json={"name": "X"})
        assert response.status_code == 404


class TestListWatchesFilter:
    async def test_filter_by_active_status(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Active Watch",
                "url": "https://example.com/active",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/api/watches/{watch_id}/deactivate")

        active = await client.get("/api/watches?is_active=true")
        inactive = await client.get("/api/watches?is_active=false")

        active_ids = [w["id"] for w in active.json()]
        inactive_ids = [w["id"] for w in inactive.json()]
        assert watch_id not in active_ids
        assert watch_id in inactive_ids


class TestDeleteWatch:
    async def _create_inactive_watch(self, client):
        """Create a watch and deactivate it; return its ID."""
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Delete Me",
                "url": "https://example.com/delete",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        await client.post(f"/api/watches/{watch_id}/deactivate")
        return watch_id

    async def test_delete_inactive_watch_returns_204(self, client):
        watch_id = await self._create_inactive_watch(client)
        response = await client.delete(f"/api/watches/{watch_id}")
        assert response.status_code == 204

    async def test_delete_watch_removes_from_db(self, client):
        watch_id = await self._create_inactive_watch(client)
        await client.delete(f"/api/watches/{watch_id}")
        response = await client.get(f"/api/watches/{watch_id}")
        assert response.status_code == 404

    async def test_delete_active_watch_returns_409(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Still Active",
                "url": "https://example.com/active",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.delete(f"/api/watches/{watch_id}")
        assert response.status_code == 409

    async def test_delete_not_found(self, client):
        response = await client.delete("/api/watches/00000000000000000000000000")
        assert response.status_code == 404

    async def test_delete_writes_audit_entry(self, client, db_session):
        watch_id = await self._create_inactive_watch(client)
        await client.delete(f"/api/watches/{watch_id}")
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "watch.deleted",
                AuditLog.payload["name"].astext == "Delete Me",
            )
        )
        entry = result.scalar_one()
        assert entry.payload["url"] == "https://example.com/delete"
        assert entry.watch_id is None  # SET NULL after cascade

    async def test_delete_cascades_children(self, client, db_session):
        """Deleting a watch cascades to all child records."""
        watch_id = await self._create_inactive_watch(client)

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
        config = NotificationConfig(
            watch_id=watch_ulid,
            channel="webhook",
        )
        db_session.add_all([chunk, profile, config])
        await db_session.flush()

        # Delete the watch
        await client.delete(f"/api/watches/{watch_id}")

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
                    select(NotificationConfig).where(NotificationConfig.watch_id == watch_ulid)
                )
            )
            .scalars()
            .all()
        )
        assert len(configs) == 0
