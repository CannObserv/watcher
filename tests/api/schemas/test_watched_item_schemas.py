"""Pydantic schema tests for WatchedItem API."""

import pytest
from pydantic import ValidationError


class TestRenameArchiversInfoItemId:
    """Regression guard: field must be archiver_info_item_id, not info_item_id."""

    def test_response_has_archiver_info_item_id(self):
        from src.api.schemas.watched_item import WatchedItemResponse

        assert "archiver_info_item_id" in WatchedItemResponse.model_fields
        assert "info_item_id" not in WatchedItemResponse.model_fields

    def test_create_accepts_archiver_info_item_id(self):
        from src.api.schemas.watched_item import WatchedItemCreate

        schema = WatchedItemCreate(archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00")
        assert schema.archiver_info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
        assert "info_item_id" not in WatchedItemCreate.model_fields


class TestWatchedItemPatch:
    def test_accepts_all_optional_fields(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch(
            name="Renamed",
            description="notes",
            default_schedule_config={"interval": "30m"},
            content_media_type="text/html",
            default_tags=["a", "b"],
        )
        assert p.name == "Renamed"
        assert p.default_schedule_config == {"interval": "30m"}
        assert p.content_media_type == "text/html"

    def test_all_fields_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        assert WatchedItemPatch().model_dump(exclude_unset=True) == {}

    def test_name_rejects_empty(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(name="")

    def test_content_media_type_free_form(self):
        # #168: raw MIME is free-form — any string within the length bound is accepted.
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch(content_media_type="application/octet-stream")
        assert p.content_media_type == "application/octet-stream"

    def test_content_media_type_rejects_overlong(self):
        from src.api.schemas.watched_item import WatchedItemPatch
        from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN

        # At the bound is accepted; one over is rejected (#168, 2048).
        WatchedItemPatch(content_media_type="x" * CONTENT_MEDIA_TYPE_MAX_LEN)
        with pytest.raises(ValidationError):
            WatchedItemPatch(content_media_type="x" * (CONTENT_MEDIA_TYPE_MAX_LEN + 1))


class TestWatchedItemResponse:
    def test_constructs_from_attributes(self):
        from datetime import UTC, datetime

        from ulid import ULID

        from src.api.schemas.watched_item import WatchedItemResponse
        from src.core.models.watched_item import WatchedItem

        wi = WatchedItem(archiver_info_item_id=ULID(), name="X")
        wi.id = ULID()
        wi.created_at = wi.updated_at = datetime.now(UTC)
        r = WatchedItemResponse.model_validate(wi)
        assert r.name == "X"

    def test_response_exposes_content_media_type_and_computed_essence(self):
        # #168: raw observed MIME is a stored field; the essence is computed.
        from src.api.schemas.watched_item import WatchedItemResponse

        assert "content_media_type" in WatchedItemResponse.model_fields
        assert "media_type_essence" in WatchedItemResponse.model_computed_fields
        assert "media_type_essence" not in WatchedItemResponse.model_fields
        assert "default_content_type" not in WatchedItemResponse.model_fields

    def test_computed_essence_is_the_resolved_dispatch_essence(self):
        """#168: media_type_essence reflects the *dispatch* resolution (tiebreaker),
        the same value the pipeline dispatches on — not just the header projection."""
        from datetime import UTC, datetime

        from ulid import ULID

        from src.api.schemas.watched_item import WatchedItemResponse
        from src.core.models.watched_item import WatchedItem

        def _essence(content_media_type, url):
            wi = WatchedItem(archiver_info_item_id=ULID(), name="X")
            wi.id = ULID()
            wi.created_at = wi.updated_at = datetime.now(UTC)
            wi.content_media_type = content_media_type
            wi.effective_url = url
            return WatchedItemResponse.model_validate(wi).media_type_essence

        # Header essence wins when informative.
        assert _essence("text/html; charset=utf-8", "https://x.gov/a") == "text/html"
        # Mislabeled header rescued by the URL-extension tiebreaker.
        assert _essence("application/octet-stream", "https://x.gov/doc.pdf") == "application/pdf"
        # Nothing informative → None (registry maps to the HTML fallback).
        assert _essence(None, "https://x.gov/page") is None


class TestIssue186SchemaAdditions:
    """#186 — new fields on WatchedItemResponse, WatchedItemCreate, WatchedItemPatch."""

    def test_response_has_health_status(self):
        from src.api.schemas.watched_item import WatchedItemResponse

        assert "health_status" in WatchedItemResponse.model_fields

    def test_response_has_last_checked_at(self):
        from src.api.schemas.watched_item import WatchedItemResponse

        assert "last_checked_at" in WatchedItemResponse.model_fields

    def test_response_has_archiver_info_source_id(self):
        from src.api.schemas.watched_item import WatchedItemResponse

        assert "archiver_info_source_id" in WatchedItemResponse.model_fields

    def test_create_accepts_archiver_info_source_id(self):
        from src.api.schemas.watched_item import WatchedItemCreate

        schema = WatchedItemCreate(
            url="https://example.com", archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00"
        )
        assert schema.archiver_info_source_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"

    def test_patch_accepts_archiver_info_source_id(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch(archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00")
        assert p.archiver_info_source_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"

    def test_patch_archiver_info_source_id_is_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch()
        assert "archiver_info_source_id" not in p.model_dump(exclude_unset=True)

    def test_create_rejects_empty_archiver_info_source_id(self):
        """#8: empty string must be rejected — semantically different from null."""
        from pydantic import ValidationError

        from src.api.schemas.watched_item import WatchedItemCreate

        with pytest.raises(ValidationError):
            WatchedItemCreate(url="https://example.com", archiver_info_source_id="")

    def test_patch_rejects_empty_archiver_info_source_id(self):
        """#8: same constraint on the patch schema."""
        from pydantic import ValidationError

        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(archiver_info_source_id="")


class TestIssue187SchemaAdditions:
    """#187 — WatchedItemPatch must expose effective_url and source_specs."""

    def test_patch_accepts_effective_url(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch(effective_url="https://example.com/new")
        assert p.effective_url == "https://example.com/new"

    def test_patch_effective_url_is_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch()
        assert "effective_url" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_empty_effective_url(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(effective_url="")

    def test_patch_accepts_source_specs(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        p = WatchedItemPatch(source_specs=specs)
        assert p.source_specs == specs

    def test_patch_source_specs_is_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch()
        assert "source_specs" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_explicit_null_effective_url(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(effective_url=None)

    def test_patch_rejects_explicit_null_source_specs(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(source_specs=None)

    def test_patch_rejects_explicit_null_name(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(name=None)


class TestIssue188IsActive:
    """#188 — is_active on WatchedItemCreate (default True) and WatchedItemPatch."""

    def test_create_defaults_is_active_true(self):
        from src.api.schemas.watched_item import WatchedItemCreate

        c = WatchedItemCreate(url="https://example.com")
        assert c.is_active is True

    def test_create_accepts_is_active_false(self):
        from src.api.schemas.watched_item import WatchedItemCreate

        c = WatchedItemCreate(url="https://example.com", is_active=False)
        assert c.is_active is False

    def test_patch_accepts_is_active(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch(is_active=False)
        assert p.is_active is False

    def test_patch_is_active_is_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        p = WatchedItemPatch()
        assert "is_active" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_explicit_null_is_active(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(is_active=None)


class TestTemplateSchemas:
    """#200 — WatchedItemTemplate* schemas folded into ItemNotificationTemplateCreate.

    The watched-item-scoped create body moved out of src.api.schemas.watched_item into
    src.api.schemas.notification_template.ItemNotificationTemplateCreate. ``visibility`` and
    ``watched_item_id`` are pinned by the route path, so the body omits them; ``title`` and
    ``remote_channel_id`` are now required and there is no body-level ``is_active``.
    """

    def test_template_create_defaults(self):
        from src.api.schemas.notification_template import ItemNotificationTemplateCreate

        c = ItemNotificationTemplateCreate(
            title="Item Template",
            remote_channel_id="01HV0000000000000000000099",
        )
        assert c.events == ["change_detected"]
        assert c.channel_hint == "remote"

    def test_template_create_rejects_empty_channel_hint(self):
        from src.api.schemas.notification_template import ItemNotificationTemplateCreate

        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(
                title="Item Template",
                remote_channel_id="01HV0000000000000000000099",
                channel_hint="",
            )

    def test_template_create_requires_title(self):
        from src.api.schemas.notification_template import ItemNotificationTemplateCreate

        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(remote_channel_id="01HV0000000000000000000099")

    def test_template_create_requires_remote_channel_id(self):
        from src.api.schemas.notification_template import ItemNotificationTemplateCreate

        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(title="Item Template")
