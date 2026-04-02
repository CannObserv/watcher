"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError
from ulid import ULID

from src.api.schemas.audit_log import AuditLogResponse
from src.api.schemas.change import (
    ChangeDetailResponse,
    ChangeResponse,
    SnapshotChunkResponse,
    SnapshotResponse,
    SnapshotWithChunksResponse,
)
from src.api.schemas.types import HttpUrlStr
from src.api.schemas.watch import WatchCreate, WatchResponse, WatchUpdate
from src.core.models.audit_log import EventType
from src.core.models.watch import ContentType, Watch


class TestHttpUrlStr:
    """Tests for the HttpUrlStr reusable type."""

    def test_valid_https_url(self):
        class M(BaseModel):
            url: HttpUrlStr

        m = M(url="https://example.com/page")
        assert m.url == "https://example.com/page"
        assert isinstance(m.url, str)

    def test_valid_http_url(self):
        class M(BaseModel):
            url: HttpUrlStr

        m = M(url="http://example.com")
        assert m.url == "http://example.com/"
        assert isinstance(m.url, str)

    def test_rejects_bare_string(self):
        class M(BaseModel):
            url: HttpUrlStr

        with pytest.raises(ValidationError):
            M(url="not-a-url")

    def test_rejects_ftp_scheme(self):
        class M(BaseModel):
            url: HttpUrlStr

        with pytest.raises(ValidationError):
            M(url="ftp://example.com/file")

    def test_normalizes_bare_domain_with_trailing_slash(self):
        """HttpUrl adds a trailing slash to bare domains — document this is intentional."""

        class M(BaseModel):
            url: HttpUrlStr

        m = M(url="https://example.com")
        assert m.url == "https://example.com/"

    def test_preserves_path_without_trailing_slash(self):
        class M(BaseModel):
            url: HttpUrlStr

        m = M(url="https://example.com/page")
        assert m.url == "https://example.com/page"

    def test_optional_field_allows_none(self):
        class M(BaseModel):
            url: HttpUrlStr | None = None

        m = M()
        assert m.url is None

    def test_optional_field_validates_when_present(self):
        class M(BaseModel):
            url: HttpUrlStr | None = None

        with pytest.raises(ValidationError):
            M(url="not-a-url")


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

    def test_watch_create_valid_ignore_patterns(self):
        data = WatchCreate(
            name="Filtered",
            url="https://example.com",
            content_type="html",
            fetch_config={"ignore_patterns": [r"\d{4}-\d{2}-\d{2}", r"foo bar"]},
        )
        assert data.fetch_config["ignore_patterns"] == [r"\d{4}-\d{2}-\d{2}", r"foo bar"]

    def test_watch_create_invalid_regex_in_ignore_patterns(self):
        with pytest.raises(ValidationError, match="not a valid regex"):
            WatchCreate(
                name="Bad",
                url="https://example.com",
                content_type="html",
                fetch_config={"ignore_patterns": [r"[invalid"]},
            )

    def test_watch_create_rejects_invalid_url(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Bad", url="not-a-url", content_type="html")

    def test_watch_create_rejects_ftp_url(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Bad", url="ftp://example.com/file", content_type="html")

    def test_watch_create_ignore_patterns_must_be_list(self):
        with pytest.raises(ValidationError, match="must be a list"):
            WatchCreate(
                name="Bad",
                url="https://example.com",
                content_type="html",
                fetch_config={"ignore_patterns": r"\d+"},
            )

    def test_watch_create_valid_ignore_selectors(self):
        data = WatchCreate(
            name="Selector Watch",
            url="https://example.com",
            content_type="html",
            fetch_config={"ignore_selectors": ["#sidebar", ".ads", "nav > ul"]},
        )
        assert data.fetch_config["ignore_selectors"] == ["#sidebar", ".ads", "nav > ul"]

    def test_watch_create_invalid_css_selector(self):
        with pytest.raises(ValidationError, match="not a valid CSS selector"):
            WatchCreate(
                name="Bad",
                url="https://example.com",
                content_type="html",
                fetch_config={"ignore_selectors": ["###invalid!!!"]},
            )

    def test_watch_create_ignore_selectors_must_be_list(self):
        with pytest.raises(ValidationError, match="must be a list"):
            WatchCreate(
                name="Bad",
                url="https://example.com",
                content_type="html",
                fetch_config={"ignore_selectors": "#sidebar"},
            )


class TestWatchUpdate:
    def test_update_partial(self):
        data = WatchUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.is_active is None

    def test_update_all_fields(self):
        data = WatchUpdate(
            name="Updated",
            content_type="pdf",
            fetch_config={"selectors": ["#main"]},
            schedule_config={"interval": "1h"},
            is_active=False,
        )
        assert data.is_active is False

    def test_update_empty_is_valid(self):
        data = WatchUpdate()
        assert data.name is None

    def test_update_url_field_not_present(self):
        """URL is intentionally omitted from WatchUpdate — immutable after creation."""
        data = WatchUpdate(name="No URL change")
        assert not hasattr(data, "url")

    def test_update_rejects_invalid_effective_url(self):
        with pytest.raises(ValidationError):
            WatchUpdate(effective_url="not-a-url")

    def test_update_accepts_valid_effective_url(self):
        data = WatchUpdate(effective_url="https://example.com/resolved")
        assert data.effective_url == "https://example.com/resolved"

    def test_update_invalid_regex_in_ignore_patterns(self):
        with pytest.raises(ValidationError, match="not a valid regex"):
            WatchUpdate(fetch_config={"ignore_patterns": [r"[bad"]})

    def test_update_none_fetch_config_is_valid(self):
        data = WatchUpdate(fetch_config=None)
        assert data.fetch_config is None

    def test_update_valid_ignore_selectors(self):
        data = WatchUpdate(fetch_config={"ignore_selectors": [".promo", "#cookie-banner"]})
        assert data.fetch_config["ignore_selectors"] == [".promo", "#cookie-banner"]

    def test_update_invalid_css_selector(self):
        with pytest.raises(ValidationError, match="not a valid CSS selector"):
            WatchUpdate(fetch_config={"ignore_selectors": ["###bad"]})


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
        assert data.screenshot_path is None
        assert data.screenshot_browser is None


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
                "significance": None,
                "detected_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH8A"
        assert data.previous_snapshot_id == "01KM7A9TP2B0BQCNZ5PZX4MH87"
        assert data.current_snapshot_id == "01KM7A9TP2B0BQCNZ5PZX4MH88"
        assert data.change_metadata == {"added": 2, "removed": 1}
        assert data.significance is None
        assert data.detected_at == ts


class TestSnapshotWithChunksResponse:
    def test_importable_from_schemas(self):
        assert SnapshotWithChunksResponse is not None

    def test_has_chunks_field(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = SnapshotWithChunksResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH87",
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "content_hash": "sha256abc",
                "simhash": 99999999,
                "storage_path": "/data/snapshots/abc.html",
                "text_path": "/data/snapshots/abc.txt",
                "storage_backend": "local",
                "chunk_count": 0,
                "text_bytes": 0,
                "fetch_duration_ms": 100,
                "fetcher_used": "http",
                "fetched_at": ts,
                "chunks": [],
            }
        )
        assert data.chunks == []


class TestChangeDetailResponse:
    def test_importable_from_schemas(self):
        assert ChangeDetailResponse is not None

    def test_has_snapshot_fields(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        snapshot_dict = {
            "id": "01KM7A9TP2B0BQCNZ5PZX4MH88",
            "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
            "content_hash": "sha256abc",
            "simhash": 99999999,
            "storage_path": "/data/snapshots/abc.html",
            "text_path": "/data/snapshots/abc.txt",
            "storage_backend": "local",
            "chunk_count": 0,
            "text_bytes": 0,
            "fetch_duration_ms": 100,
            "fetcher_used": "http",
            "fetched_at": ts,
            "chunks": [],
        }
        data = ChangeDetailResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH8A",
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "previous_snapshot_id": "01KM7A9TP2B0BQCNZ5PZX4MH87",
                "current_snapshot_id": "01KM7A9TP2B0BQCNZ5PZX4MH88",
                "change_metadata": {},
                "significance": None,
                "detected_at": ts,
                "current_snapshot": snapshot_dict,
                "previous_snapshot": None,
            }
        )
        assert data.current_snapshot is not None
        assert data.current_snapshot.id == "01KM7A9TP2B0BQCNZ5PZX4MH88"
        assert data.previous_snapshot is None


class TestWatchResponse:
    def test_watch_response_includes_is_archived(self):
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        watch.id = ULID()
        watch.created_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        watch.updated_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        response = WatchResponse.model_validate(watch)
        assert response.is_archived is False

    def test_watch_response_is_archived_true(self):
        watch = Watch(
            name="Archived",
            url="https://example.com",
            content_type=ContentType.HTML,
            is_archived=True,
        )
        watch.id = ULID()
        watch.created_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        watch.updated_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        response = WatchResponse.model_validate(watch)
        assert response.is_archived is True


class TestAuditLogResponse:
    def test_from_dict(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = AuditLogResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH8B",
                "event_type": EventType.WATCH_CREATED,
                "watch_id": "01KM7A9TP2B0BQCNZ5PZX4MH89",
                "payload": {"name": "Test Watch"},
                "created_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH8B"
        assert data.event_type == EventType.WATCH_CREATED
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
