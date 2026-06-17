"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

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
from src.api.schemas.watched_item import WatchedItemCreate
from src.core.models.audit_log import EventType


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


class TestAuditLogResponse:
    def test_from_dict(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = AuditLogResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5PZX4MH8B",
                "event_type": EventType.WATCHED_ITEM_CREATED,
                "payload": {"watched_item_id": "01KM7A9TP2B0BQCNZ5PZX4MH89", "name": "Test Item"},
                "created_at": ts,
            }
        )
        assert data.id == "01KM7A9TP2B0BQCNZ5PZX4MH8B"
        assert data.event_type == EventType.WATCHED_ITEM_CREATED
        assert data.payload["watched_item_id"] == "01KM7A9TP2B0BQCNZ5PZX4MH89"
        assert data.created_at == ts

    def test_empty_payload(self):
        ts = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        data = AuditLogResponse.model_validate(
            {
                "id": "01KM7A9TP2B0BQCNZ5Q0000000",
                "event_type": "system.startup",
                "payload": {},
                "created_at": ts,
            }
        )
        assert data.payload == {}


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
                "watched_item_id": "01HV0000000000000000000002",
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
                "watched_item_id": "01HV0000000000000000000002",
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


class TestWatchedItemCreate:
    def test_watched_item_create_requires_archiver_info_item_id_or_url(self):
        with pytest.raises(ValidationError):
            WatchedItemCreate(name="X")

    def test_watched_item_create_minimal_ok(self):
        schema = WatchedItemCreate(archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00")
        assert schema.archiver_info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
        assert schema.name is None
        assert schema.default_tags is None

    def test_watched_item_create_full_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            name="Custom Name",
            description="Note",
            default_schedule_config={"interval": "15m"},
            default_content_type="html",
            default_tags=["regulatory"],
        )
        assert schema.default_content_type == "html"

    def test_watched_item_create_rejects_invalid_content_type(self):
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                default_content_type="garbage",
            )

    def test_watched_item_create_none_content_type_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            default_content_type=None,
        )
        assert schema.default_content_type is None
