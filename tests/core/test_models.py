"""Tests for SQLAlchemy base and ULID column type."""

import pytest
from ulid import ULID

from src.core.database import get_database_url, get_engine, reset_engine
from src.core.models.audit_log import AuditLog
from src.core.models.base import ULIDType
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.watch import ContentType, Watch


class TestULIDType:
    def test_process_bind_param_converts_ulid_to_string(self):
        ulid_type = ULIDType()
        value = ULID()
        result = ulid_type.process_bind_param(value, dialect=None)
        assert isinstance(result, str)
        assert result == str(value)

    def test_process_bind_param_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_bind_param(None, dialect=None)
        assert result is None

    def test_process_result_value_converts_string_to_ulid(self):
        ulid_type = ULIDType()
        original = ULID()
        result = ulid_type.process_result_value(str(original), dialect=None)
        assert isinstance(result, ULID)
        assert result == original

    def test_process_result_value_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_result_value(None, dialect=None)
        assert result is None


class TestWatchModel:
    def test_create_watch_with_defaults(self):
        watch = Watch(
            name="Test Watch",
            url="https://example.com/agenda",
            content_type=ContentType.HTML,
        )
        assert watch.name == "Test Watch"
        assert watch.url == "https://example.com/agenda"
        assert watch.content_type == ContentType.HTML
        assert watch.is_active is True
        assert watch.fetch_config == {}
        assert watch.schedule_config == {}

    def test_create_watch_with_all_fields(self):
        watch = Watch(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type=ContentType.PDF,
            fetch_config={"selectors": ["#content"]},
            schedule_config={"interval": "6h"},
            is_active=False,
        )
        assert watch.content_type == ContentType.PDF
        assert watch.fetch_config == {"selectors": ["#content"]}
        assert watch.schedule_config == {"interval": "6h"}
        assert watch.is_active is False

    def test_content_type_enum_values(self):
        assert ContentType.HTML.value == "html"
        assert ContentType.PDF.value == "pdf"
        assert ContentType.FILE.value == "file"

    def test_content_type_coerces_string(self):
        watch = Watch(
            name="Coerce Test",
            url="https://example.com",
            content_type="pdf",
        )
        assert watch.content_type is ContentType.PDF

    def test_content_type_rejects_invalid(self):
        with pytest.raises(ValueError, match="Invalid content_type"):
            Watch(
                name="Bad Type",
                url="https://example.com",
                content_type="invalid",
            )


class TestAuditLogModel:
    def test_create_audit_log_entry(self):
        entry = AuditLog(
            event_type="watch.created",
            payload={"watch_name": "Test Watch"},
        )
        assert entry.event_type == "watch.created"
        assert entry.payload == {"watch_name": "Test Watch"}
        assert entry.watch_id is None

    def test_create_audit_log_with_watch_id(self):
        watch_id = ULID()
        entry = AuditLog(
            event_type="check.started",
            watch_id=watch_id,
            payload={"url": "https://example.com"},
        )
        assert entry.watch_id == watch_id


class TestDatabase:
    def test_get_database_url_raises_without_env(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL environment variable is not set"):
            get_database_url()

    def test_get_database_url_from_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://custom:pass@db:5432/mydb")
        url = get_database_url()
        assert url == "postgresql+asyncpg://custom:pass@db:5432/mydb"

    def test_reset_engine_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/test")
        reset_engine()
        engine = get_engine()
        assert engine is not None
        reset_engine()
        # After reset, a new engine should be created on next call
        engine2 = get_engine()
        assert engine2 is not engine


class TestSnapshotModel:
    def test_create_snapshot(self):
        snap = Snapshot(
            watch_id=ULID(),
            content_hash="abc123",
            simhash=42,
            storage_path="snapshots/w1/s1.html",
            text_path="snapshots/w1/s1.txt",
            storage_backend="local",
            chunk_count=3,
            text_bytes=1024,
            fetch_duration_ms=200,
            fetcher_used="http",
        )
        assert snap.content_hash == "abc123"
        assert snap.chunk_count == 3
        assert snap.storage_backend == "local"


class TestSnapshotChunkModel:
    def test_create_chunk(self):
        chunk = SnapshotChunk(
            snapshot_id=ULID(),
            chunk_index=0,
            chunk_type="page",
            chunk_label="Page 1",
            content_hash="def456",
            simhash=99,
            char_count=500,
            excerpt="First 500 chars...",
        )
        assert chunk.chunk_index == 0
        assert chunk.chunk_type == "page"


class TestChangeModel:
    def test_create_change(self):
        change = Change(
            watch_id=ULID(),
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
            change_metadata={"modified": [{"index": 0, "label": "Page 1"}]},
        )
        assert change.change_metadata["modified"][0]["label"] == "Page 1"
