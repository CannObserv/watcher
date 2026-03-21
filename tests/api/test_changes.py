"""Integration tests for Changes API endpoints."""

import pytest

from src.core.models.base import generate_ulid
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch

pytestmark = pytest.mark.integration


@pytest.fixture
async def watch_with_changes(db_session):
    """Create a Watch with two Snapshots, chunks on snap2, and a Change."""
    watch = Watch(
        name="Change Test Watch",
        url="https://example.com/changes",
        content_type=ContentType.HTML,
    )
    db_session.add(watch)
    await db_session.flush()

    snap1 = Snapshot(
        id=generate_ulid(),
        watch_id=watch.id,
        content_hash="aaa111",
        simhash=100,
        storage_path="/data/snap1.raw",
        text_path="/data/snap1.txt",
        storage_backend="local",
        chunk_count=0,
        text_bytes=500,
        fetch_duration_ms=120,
        fetcher_used="http",
    )
    snap2 = Snapshot(
        id=generate_ulid(),
        watch_id=watch.id,
        content_hash="bbb222",
        simhash=200,
        storage_path="/data/snap2.raw",
        text_path="/data/snap2.txt",
        storage_backend="local",
        chunk_count=2,
        text_bytes=800,
        fetch_duration_ms=150,
        fetcher_used="http",
    )
    db_session.add_all([snap1, snap2])
    await db_session.flush()

    chunk1 = SnapshotChunk(
        id=generate_ulid(),
        snapshot_id=snap2.id,
        chunk_index=0,
        chunk_type="section",
        chunk_label="Header",
        content_hash="ccc333",
        simhash=300,
        char_count=100,
        excerpt="First chunk excerpt",
    )
    chunk2 = SnapshotChunk(
        id=generate_ulid(),
        snapshot_id=snap2.id,
        chunk_index=1,
        chunk_type="section",
        chunk_label="Body",
        content_hash="ddd444",
        simhash=400,
        char_count=200,
        excerpt="Second chunk excerpt",
    )
    db_session.add_all([chunk1, chunk2])
    await db_session.flush()

    change = Change(
        id=generate_ulid(),
        watch_id=watch.id,
        previous_snapshot_id=snap1.id,
        current_snapshot_id=snap2.id,
        change_metadata={"chunks_added": 2},
    )
    db_session.add(change)
    await db_session.flush()

    return {
        "watch": watch,
        "snap1": snap1,
        "snap2": snap2,
        "chunks": [chunk1, chunk2],
        "change": change,
    }


class TestListChanges:
    async def test_list_all_changes(self, client, watch_with_changes):
        response = await client.get("/api/changes")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        change_ids = [c["id"] for c in data]
        assert str(watch_with_changes["change"].id) in change_ids

    async def test_filter_by_watch_id(self, client, watch_with_changes):
        watch_id = str(watch_with_changes["watch"].id)
        response = await client.get(f"/api/changes?watch_id={watch_id}")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert all(c["watch_id"] == watch_id for c in data)

    async def test_filter_by_nonexistent_watch(self, client, watch_with_changes):
        response = await client.get(
            "/api/changes?watch_id=00000000000000000000000000"
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_pagination(self, client, watch_with_changes):
        response = await client.get("/api/changes?limit=1&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert len(data) <= 1


class TestGetChangeDetail:
    async def test_get_change_with_chunks(self, client, watch_with_changes):
        change_id = str(watch_with_changes["change"].id)
        response = await client.get(f"/api/changes/{change_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == change_id
        assert data["current_snapshot"] is not None
        assert len(data["current_snapshot"]["chunks"]) == 2
        # Chunks ordered by chunk_index
        assert data["current_snapshot"]["chunks"][0]["chunk_label"] == "Header"
        assert data["current_snapshot"]["chunks"][1]["chunk_label"] == "Body"
        # Previous snapshot exists but has no chunks
        assert data["previous_snapshot"] is not None
        assert data["previous_snapshot"]["chunks"] == []

    async def test_get_nonexistent_change(self, client, watch_with_changes):
        response = await client.get(
            "/api/changes/00000000000000000000000000"
        )
        assert response.status_code == 404
