"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError
from ulid import ULID

from src.api.schemas.audit_log import AuditLogResponse
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
        source_id = str(ULID())
        data = WatchCreate(
            name="Test Watch",
            info_item_id=str(ULID()),
            info_source_id=source_id,
            content_type="html",
        )
        assert data.name == "Test Watch"
        assert data.info_source_id == source_id
        assert data.content_type == "html"
        assert data.schedule_config == {}

    def test_watch_create_requires_name(self):
        with pytest.raises(ValidationError):
            WatchCreate(info_item_id=str(ULID()), info_source_id=str(ULID()), content_type="html")

    def test_watch_create_requires_info_source_id(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", info_item_id=str(ULID()), content_type="html")

    def test_watch_create_validates_content_type(self):
        with pytest.raises(ValidationError):
            WatchCreate(
                name="Test",
                info_item_id=str(ULID()),
                info_source_id=str(ULID()),
                content_type="invalid",
            )

    def test_watch_create_with_schedule_config(self):
        data = WatchCreate(
            name="PDF Watch",
            info_item_id=str(ULID()),
            info_source_id=str(ULID()),
            content_type="pdf",
            schedule_config={"interval": "6h"},
        )
        assert data.schedule_config == {"interval": "6h"}

    def test_watch_create_no_legacy_fields(self):
        """``url`` and ``fetch_config`` no longer accepted (silently ignored)."""
        data = WatchCreate(
            name="Silent",
            info_item_id=str(ULID()),
            info_source_id=str(ULID()),
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


# Phase 5 (#156): TestSnapshotChunkResponse, TestSnapshotResponse, TestChangeResponse,
# TestSnapshotWithChunksResponse, TestChangeDetailResponse removed.
# src/api/schemas/change.py deleted; Snapshot/Change tables dropped.


class TestWatchResponse:
    def _build_watch(self, **overrides):
        watch = Watch(
            name=overrides.pop("name", "Test"),
            info_source_id=ULID(),
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

    def test_watch_response_has_info_source_id(self):
        """Phase 5: WatchResponse exposes info_source_id (not info_item_id)."""
        watch = self._build_watch()
        response = WatchResponse.model_validate(watch)
        assert response.info_source_id == str(watch.info_source_id)

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
