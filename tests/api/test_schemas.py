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
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.api.schemas.notification_config import (
    WatchNotificationConfigCreate,
    WatchNotificationConfigResponse,
)
from src.api.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplateResponse,
)
from src.api.schemas.types import HttpUrlStr
from src.api.schemas.validators import validate_event_list
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
        info_id = str(ULID())
        data = WatchCreate(
            name="Test Watch",
            info_item_id=info_id,
            content_type="html",
        )
        assert data.name == "Test Watch"
        assert data.info_item_id == info_id
        assert data.content_type == "html"
        assert data.schedule_config == {}

    def test_watch_create_requires_name(self):
        with pytest.raises(ValidationError):
            WatchCreate(info_item_id=str(ULID()), content_type="html")

    def test_watch_create_requires_info_item_id(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", content_type="html")

    def test_watch_create_validates_content_type(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", info_item_id=str(ULID()), content_type="invalid")

    def test_watch_create_with_schedule_config(self):
        data = WatchCreate(
            name="PDF Watch",
            info_item_id=str(ULID()),
            content_type="pdf",
            schedule_config={"interval": "6h"},
        )
        assert data.schedule_config == {"interval": "6h"}

    def test_watch_create_no_legacy_fields(self):
        """``url`` and ``fetch_config`` no longer accepted (silently ignored)."""
        info_id = str(ULID())
        data = WatchCreate(
            name="Silent",
            info_item_id=info_id,
            content_type="html",
            # These extra keys must be ignored or rejected, never stored.
        )
        assert not hasattr(data, "url")
        assert not hasattr(data, "fetch_config")


class TestWatchUpdate:
    def test_update_partial(self):
        data = WatchUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.is_active is None

    def test_update_empty_is_valid(self):
        data = WatchUpdate()
        assert data.name is None

    def test_update_url_field_not_present(self):
        """URL is intentionally omitted from WatchUpdate — owned by InfoSpec."""
        data = WatchUpdate(name="No URL change")
        assert not hasattr(data, "url")

    def test_update_no_fetch_config_field(self):
        """fetch_config is owned by the InfoSpec; never on the watch row."""
        data = WatchUpdate(name="X")
        assert not hasattr(data, "fetch_config")

    def test_update_rejects_invalid_effective_url(self):
        with pytest.raises(ValidationError):
            WatchUpdate(effective_url="not-a-url")

    def test_update_accepts_valid_effective_url(self):
        data = WatchUpdate(effective_url="https://example.com/resolved")
        assert data.effective_url == "https://example.com/resolved"


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
    def _build_watch(self, **overrides):
        watch = Watch(
            name=overrides.pop("name", "Test"),
            info_item_id=ULID(),
            content_type=overrides.pop("content_type", ContentType.HTML),
            **overrides,
        )
        watch.id = ULID()
        watch.created_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        watch.updated_at = datetime(2026, 3, 20, 0, 0, 0, tzinfo=UTC)
        return watch

    def test_watch_response_includes_is_archived(self):
        watch = self._build_watch()
        response = WatchResponse.model_validate(watch)
        assert response.is_archived is False

    def test_watch_response_is_archived_true(self):
        watch = self._build_watch(name="Archived", is_archived=True)
        response = WatchResponse.model_validate(watch)
        assert response.is_archived is True

    def test_watch_response_has_info_item_id(self):
        watch = self._build_watch()
        response = WatchResponse.model_validate(watch)
        assert response.info_item_id == str(watch.info_item_id)

    def test_watch_response_has_no_legacy_url_field(self):
        """Phase 2c: WatchResponse must not expose ``url`` (now in InfoSpec)."""
        watch = self._build_watch()
        response = WatchResponse.model_validate(watch)
        # model_dump must not contain ``url`` or ``fetch_config`` keys.
        dumped = response.model_dump()
        assert "url" not in dumped
        assert "fetch_config" not in dumped


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


class TestValidateEventList:
    def test_valid_events_returned_unchanged(self):
        result = validate_event_list(["change_detected", "watch_error"])
        assert result == ["change_detected", "watch_error"]

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="At least one event"):
            validate_event_list([])

    def test_single_valid_event(self):
        assert validate_event_list(["watch_created"]) == ["watch_created"]

    def test_unknown_event_raises(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            validate_event_list(["not_a_real_event"])

    def test_mixed_valid_and_invalid_raises(self):
        with pytest.raises(ValueError, match="not_a_real_event"):
            validate_event_list(["change_detected", "not_a_real_event"])

    def test_error_message_names_invalid_events(self):
        with pytest.raises(ValueError, match=r"\['bad_event'\]"):
            validate_event_list(["bad_event"])


class TestWatchNotificationConfigCreate:
    def test_content_config_accepted(self):
        """WatchNotificationConfigCreate accepts content_config and it's accessible."""
        schema = WatchNotificationConfigCreate(
            remote_channel_id="01HV0000000000000000000099",
            content_config=ContentConfig(default=ContentOptions(include_domain=True)),
        )
        assert schema.content_config is not None
        assert schema.content_config.default.include_domain is True

    def test_content_config_optional(self):
        """content_config defaults to None."""
        schema = WatchNotificationConfigCreate(
            remote_channel_id="01HV0000000000000000000099",
        )
        assert schema.content_config is None


class TestWatchNotificationConfigResponse:
    def test_content_config_deserializes_from_dict(self):
        """WatchNotificationConfigResponse deserialises content_config from dict (ORM output)."""
        resp = WatchNotificationConfigResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "watch_id": "01HV0000000000000000000002",
                "title": None,
                "channel_hint": "slack",
                "events": ["change_detected"],
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "content_config": {"default": {"include_domain": True}, "overrides": {}},
            }
        )
        assert resp.content_config is not None
        assert resp.content_config.default.include_domain is True

    def test_content_config_null(self):
        """content_config can be null in response."""
        resp = WatchNotificationConfigResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "watch_id": "01HV0000000000000000000002",
                "title": None,
                "channel_hint": "slack",
                "events": ["change_detected"],
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "content_config": None,
            }
        )
        assert resp.content_config is None


class TestNotificationTemplateCreate:
    def test_content_config_accepted(self):
        """NotificationTemplateCreate accepts content_config."""
        schema = NotificationTemplateCreate(
            title="My Template",
            remote_channel_id="01HV0000000000000000000099",
            content_config=ContentConfig(default=ContentOptions(include_diff_snippet=True)),
        )
        assert schema.content_config is not None
        assert schema.content_config.default.include_diff_snippet is True

    def test_content_config_optional(self):
        """content_config defaults to None."""
        schema = NotificationTemplateCreate(
            title="My Template",
            remote_channel_id="01HV0000000000000000000099",
        )
        assert schema.content_config is None


class TestNotificationTemplateResponse:
    def test_content_config_deserializes_from_dict(self):
        """NotificationTemplateResponse deserialises content_config from dict."""
        resp = NotificationTemplateResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "title": "Template",
                "channel_hint": "slack",
                "events": ["change_detected"],
                "is_global_default": False,
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "watch_ref_count": 0,
                "domain_ref_count": 0,
                "content_config": {
                    "default": {"include_diff_snippet": True, "diff_snippet_lines": 5},
                    "overrides": {},
                },
            }
        )
        assert resp.content_config is not None
        assert resp.content_config.default.include_diff_snippet is True
        assert resp.content_config.default.diff_snippet_lines == 5

    def test_content_config_null(self):
        """content_config can be null in response."""
        resp = NotificationTemplateResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "title": "Template",
                "channel_hint": "slack",
                "events": ["change_detected"],
                "is_global_default": False,
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "watch_ref_count": 0,
                "domain_ref_count": 0,
                "content_config": None,
            }
        )
        assert resp.content_config is None
