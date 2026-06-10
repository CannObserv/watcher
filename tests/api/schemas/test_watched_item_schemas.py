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
            default_content_type="html",
            default_tags=["a", "b"],
        )
        assert p.name == "Renamed"
        assert p.default_schedule_config == {"interval": "30m"}

    def test_all_fields_optional(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        assert WatchedItemPatch().model_dump(exclude_unset=True) == {}

    def test_name_rejects_empty(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(name="")

    def test_invalid_content_type(self):
        from src.api.schemas.watched_item import WatchedItemPatch

        with pytest.raises(ValidationError):
            WatchedItemPatch(default_content_type="bogus")


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


class TestTemplateSchemas:
    def test_template_create_defaults(self):
        from src.api.schemas.watched_item import WatchedItemTemplateCreate

        c = WatchedItemTemplateCreate(channel_hint="mailto://x@y.z")
        assert c.events == ["change_detected"]
        assert c.is_active is True

    def test_template_create_rejects_empty_channel(self):
        from src.api.schemas.watched_item import WatchedItemTemplateCreate

        with pytest.raises(ValidationError):
            WatchedItemTemplateCreate(channel_hint="")
