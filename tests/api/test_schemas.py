"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from src.api.schemas.audit_log import AuditLogResponse
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.api.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplateResponse,
)
from src.api.schemas.types import HttpUrlStr
from src.api.schemas.watched_item import WatchedItemCreate
from src.core.models.audit_log import EventType
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
)


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


# TestWatchNotificationConfigCreate / TestWatchNotificationConfigResponse removed (#200):
# src.api.schemas.notification_config (WatchNotificationConfig* schemas) was deleted when the
# five legacy dispatch sources collapsed into the single visibility-scoped NotificationTemplate.


class TestNotificationTemplateCreate:
    def test_content_config_accepted(self):
        """NotificationTemplateCreate accepts content_config."""
        schema = NotificationTemplateCreate(
            title="My Template",
            remote_channel_id="01HV0000000000000000000099",
            content_config=ContentConfig(default=ContentOptions(include_domain=True)),
        )
        assert schema.content_config is not None
        assert schema.content_config.default.include_domain is True

    def test_content_config_optional(self):
        """content_config defaults to None."""
        schema = NotificationTemplateCreate(
            title="My Template",
            remote_channel_id="01HV0000000000000000000099",
        )
        assert schema.content_config is None

    def test_defaults_to_global_visibility(self):
        """visibility defaults to 'global' with no scope refs (#200)."""
        schema = NotificationTemplateCreate(
            title="My Template",
            remote_channel_id="01HV0000000000000000000099",
        )
        assert schema.visibility == VISIBILITY_GLOBAL
        assert schema.domain_name is None
        assert schema.watched_item_id is None

    def test_title_required(self):
        """title is mandatory post-#200."""
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(remote_channel_id="01HV0000000000000000000099")

    def test_title_rejects_over_100_chars(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="x" * 101,
                remote_channel_id="01HV0000000000000000000099",
            )

    def test_remote_channel_id_required_len26(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(title="T", remote_channel_id="too-short")

    def test_global_rejects_scope_refs(self):
        """global visibility must not set domain_name or watched_item_id (#200)."""
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility=VISIBILITY_GLOBAL,
                domain_name="example.com",
            )

    def test_domain_visibility_valid(self):
        """domain visibility requires domain_name and no watched_item_id (#200)."""
        schema = NotificationTemplateCreate(
            title="T",
            remote_channel_id="01HV0000000000000000000099",
            visibility=VISIBILITY_DOMAIN,
            domain_name="example.com",
        )
        assert schema.visibility == VISIBILITY_DOMAIN
        assert schema.domain_name == "example.com"
        assert schema.watched_item_id is None

    def test_domain_visibility_requires_domain_name(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility=VISIBILITY_DOMAIN,
            )

    def test_domain_visibility_rejects_watched_item_id(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility=VISIBILITY_DOMAIN,
                domain_name="example.com",
                watched_item_id="01HV0000000000000000000002",
            )

    def test_watched_item_visibility_valid(self):
        """watched_item visibility requires watched_item_id and no domain_name (#200)."""
        schema = NotificationTemplateCreate(
            title="T",
            remote_channel_id="01HV0000000000000000000099",
            visibility=VISIBILITY_WATCHED_ITEM,
            watched_item_id="01HV0000000000000000000002",
        )
        assert schema.visibility == VISIBILITY_WATCHED_ITEM
        assert schema.watched_item_id == "01HV0000000000000000000002"
        assert schema.domain_name is None

    def test_watched_item_visibility_requires_watched_item_id(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility=VISIBILITY_WATCHED_ITEM,
            )

    def test_watched_item_visibility_rejects_domain_name(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility=VISIBILITY_WATCHED_ITEM,
                watched_item_id="01HV0000000000000000000002",
                domain_name="example.com",
            )

    def test_rejects_unknown_visibility(self):
        with pytest.raises(ValidationError):
            NotificationTemplateCreate(
                title="T",
                remote_channel_id="01HV0000000000000000000099",
                visibility="bogus",
            )


class TestNotificationTemplateResponse:
    def test_content_config_deserializes_from_dict(self):
        """NotificationTemplateResponse deserialises content_config from dict."""
        resp = NotificationTemplateResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "title": "Template",
                "channel_hint": "slack",
                "events": ["change_detected"],
                "visibility": VISIBILITY_GLOBAL,
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "content_config": {
                    "default": {"include_domain": True, "include_tags": True},
                    "overrides": {},
                },
            }
        )
        assert resp.content_config is not None
        assert resp.content_config.default.include_domain is True
        assert resp.content_config.default.include_tags is True

    def test_content_config_null(self):
        """content_config can be null in response."""
        resp = NotificationTemplateResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "title": "Template",
                "channel_hint": "slack",
                "events": ["change_detected"],
                "visibility": VISIBILITY_GLOBAL,
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "content_config": None,
            }
        )
        assert resp.content_config is None

    def test_exposes_visibility_and_scope_refs(self):
        """visibility/domain_name/watched_item_id replace is_global_default + ref counts (#200)."""
        resp = NotificationTemplateResponse.model_validate(
            {
                "id": "01HV0000000000000000000001",
                "title": "Template",
                "channel_hint": "slack",
                "events": ["change_detected"],
                "visibility": VISIBILITY_DOMAIN,
                "domain_name": "example.com",
                "watched_item_id": None,
                "is_active": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "content_config": None,
                "remote_channel_id": "01HV0000000000000000000099",
            }
        )
        assert resp.visibility == VISIBILITY_DOMAIN
        assert resp.domain_name == "example.com"
        assert resp.watched_item_id is None
        assert resp.remote_channel_id == "01HV0000000000000000000099"
        # Removed fields must no longer be model fields.
        assert "is_global_default" not in NotificationTemplateResponse.model_fields
        assert "watch_ref_count" not in NotificationTemplateResponse.model_fields
        assert "domain_ref_count" not in NotificationTemplateResponse.model_fields


class TestWatchedItemCreate:
    def test_watched_item_create_requires_every_archiver_link(self):
        """#251: the InfoItem link, its URL, and the InfoSource link are all required."""
        with pytest.raises(ValidationError):
            WatchedItemCreate(name="X")
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00", url="https://example.com"
            )
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            )

    def test_watched_item_create_minimal_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        )
        assert schema.archiver_info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
        assert schema.name is None
        assert schema.default_tags is None

    def test_watched_item_create_full_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            name="Custom Name",
            description="Note",
            default_schedule_config={"interval": "15m"},
            content_media_type="text/html",
            default_tags=["regulatory"],
        )
        assert schema.content_media_type == "text/html"

    def test_watched_item_create_accepts_arbitrary_media_type(self):
        # Free-form raw MIME (#168) — no enum membership check; only a length bound.
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            content_media_type="application/vnd.custom+json",
        )
        assert schema.content_media_type == "application/vnd.custom+json"

    def test_watched_item_create_none_content_media_type_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            content_media_type=None,
        )
        assert schema.content_media_type is None
