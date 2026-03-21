"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.audit_log import AuditLogResponse
from src.api.schemas.change import (
    ChangeResponse,
    SnapshotChunkResponse,
    SnapshotResponse,
)
from src.api.schemas.watch import WatchCreate, WatchUpdate


class TestWatchCreate:
    def test_valid_watch_create(self):
        data = WatchCreate(
            name="Test Watch",
            url="https://example.com/page",
            content_type="html",
        )
        assert data.name == "Test Watch"
        assert data.url == "https://example.com/page"
        assert data.content_type == "html"
        assert data.fetch_config == {}
        assert data.schedule_config == {}

    def test_watch_create_requires_name(self):
        with pytest.raises(ValidationError):
            WatchCreate(url="https://example.com", content_type="html")

    def test_watch_create_requires_url(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", content_type="html")

    def test_watch_create_validates_content_type(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", url="https://example.com", content_type="invalid")

    def test_watch_create_with_configs(self):
        data = WatchCreate(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type="pdf",
            fetch_config={"timeout": 30},
            schedule_config={"interval": "6h"},
        )
        assert data.fetch_config == {"timeout": 30}
        assert data.schedule_config == {"interval": "6h"}


class TestWatchUpdate:
    def test_update_partial(self):
        data = WatchUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.url is None
        assert data.is_active is None

    def test_update_all_fields(self):
        data = WatchUpdate(
            name="Updated",
            url="https://new.example.com",
            content_type="pdf",
            fetch_config={"selectors": ["#main"]},
            schedule_config={"interval": "1h"},
            is_active=False,
        )
        assert data.is_active is False

    def test_update_empty_is_valid(self):
        data = WatchUpdate()
        assert data.name is None


class TestSnapshotChunkResponse:
    def test_from_dict(self):
        data = SnapshotChunkResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH86",
                "snapshot_id": "01KM7A9TP2B0BQCNZ5PZX4MH87",
                "chunk_index": 0,
                "chunk_type": "text",
                "chunk_label": "section-1",
                "content_hash": "abc123",
                "simhash": 12345678,
                "char_count": 500,
                "excerpt": "First 200 chars...",
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH86"
        assert data.snapshot_id == "01KM7A9TP2B0BQCNZ5PZX4MH87"
        assert data.chunk_index == 0
        assert data.chunk_type == "text"
        assert data.chunk_label == "section-1"
        assert data.content_hash == "abc123"
        assert data.simhash == 12345678
        assert data.char_count == 500
        assert data.excerpt == "First 200 chars..."


class TestSnapshotResponse:
    def test_from_dict(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = SnapshotResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH87",
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "content_hash": "sha256abc",
                "simhash": 99999999,
                "storage_path": "/data/snapshots/abc.html",
                "text_path": "/data/snapshots/abc.txt",
                "storage_backend": "local",
                "chunk_count": 3,
                "text_bytes": 4096,
                "fetch_duration_ms": 250,
                "fetcher_used": "http",
                "fetched_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH87"
        assert data.watch_id == "01KM7A9TP2B0BQCNZ5PZX4MH89"
        assert data.content_hash == "sha256abc"
        assert data.chunk_count == 3
        assert data.fetched_at == ts


class TestChangeResponse:
    def test_from_dict(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = ChangeResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH8A",
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "previous_snapshot_id": "01KM7A9TP2B0BQCNZ5PZX4MH87",
                "current_snapshot_id": "01KM7A9TP2B0BQCNZ5PZX4MH88",
                "change_metadata": {"added": 2, "removed": 1},
                "detected_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH8A"
        assert data.previous_snapshot_id == "01KM7A9TP2B0BQCNZ5PZX4MH87"
        assert data.current_snapshot_id == "01KM7A9TP2B0BQCNZ5PZX4MH88"
        assert data.change_metadata == {"added": 2, "removed": 1}
        assert data.detected_at == ts


class TestAuditLogResponse:
    def test_from_dict(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = AuditLogResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH8B",
                "event_type": "watch.created",
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "payload": {"name": "Test Watch"},
                "created_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH8B"
        assert data.event_type == "watch.created"
        assert data.watch_id == "01KM7A9TP2B0BQCNZ5PZX4MH89"
        assert data.payload == {"name": "Test Watch"}
        assert data.created_at == ts

    def test_nullable_watch_id(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = AuditLogResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5Q0000000",
                "event_type": "system.startup",
                "watch_id": None,
                "payload": {},
                "created_at": ts,
            }
        )
        assert data.watch_id is None
