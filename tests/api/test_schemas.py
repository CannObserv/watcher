"""Tests for Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from src.api.schemas.audit_log import AuditLogResponse
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


# Required and non-empty on create since #260.
_SPECS = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]


class TestWatchedItemCreate:
    def test_watched_item_create_requires_every_archiver_link(self):
        """#251: the InfoItem link, its URL, and the InfoSource link are all required."""
        with pytest.raises(ValidationError):
            WatchedItemCreate(name="X")
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                url="https://example.com",
                source_specs=_SPECS,
            )
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                source_specs=_SPECS,
            )

    def test_watched_item_create_minimal_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
        )
        assert schema.archiver_info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
        assert schema.name is None
        assert schema.default_tags is None

    def test_watched_item_create_full_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
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
            source_specs=_SPECS,
            content_media_type="application/vnd.custom+json",
        )
        assert schema.content_media_type == "application/vnd.custom+json"

    def test_watched_item_create_none_content_media_type_ok(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
            content_media_type=None,
        )
        assert schema.content_media_type is None
