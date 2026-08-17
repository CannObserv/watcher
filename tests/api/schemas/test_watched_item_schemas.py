"""Pydantic schema tests for WatchedItem API."""

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from ulid import ULID

from src.api.main import app
from src.api.schemas.notification_template import ItemNotificationTemplateCreate
from src.api.schemas.types import ULID_PATTERN
from src.api.schemas.watched_item import (
    WatchedItemCreate,
    WatchedItemPatch,
    WatchedItemResponse,
)
from src.core.models.watched_item import CONTENT_MEDIA_TYPE_MAX_LEN, WatchedItem

# Required and non-empty on create since #260; every construction needs one.
_SPECS = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]


class TestRenameArchiversInfoItemId:
    """Regression guard: field must be archiver_info_item_id, not info_item_id."""

    def test_response_has_archiver_info_item_id(self):
        assert "archiver_info_item_id" in WatchedItemResponse.model_fields
        assert "info_item_id" not in WatchedItemResponse.model_fields

    def test_create_accepts_archiver_info_item_id(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
        )
        assert schema.archiver_info_item_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"
        assert "info_item_id" not in WatchedItemCreate.model_fields


class TestWatchedItemPatch:
    def test_accepts_all_optional_fields(self):
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
        assert WatchedItemPatch().model_dump(exclude_unset=True) == {}

    def test_name_rejects_empty(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(name="")

    def test_content_media_type_free_form(self):
        # #168: raw MIME is free-form — any string within the length bound is accepted.
        p = WatchedItemPatch(content_media_type="application/octet-stream")
        assert p.content_media_type == "application/octet-stream"

    def test_content_media_type_rejects_overlong(self):
        # At the bound is accepted; one over is rejected (#168, 2048).
        WatchedItemPatch(content_media_type="x" * CONTENT_MEDIA_TYPE_MAX_LEN)
        with pytest.raises(ValidationError):
            WatchedItemPatch(content_media_type="x" * (CONTENT_MEDIA_TYPE_MAX_LEN + 1))


class TestWatchedItemResponse:
    def test_constructs_from_attributes(self):
        wi = WatchedItem(
            archiver_info_source_id=str(ULID()), archiver_info_item_id=ULID(), name="X"
        )
        wi.id = ULID()
        wi.created_at = wi.updated_at = datetime.now(UTC)
        r = WatchedItemResponse.model_validate(wi)
        assert r.name == "X"

    def test_response_exposes_content_media_type_and_computed_essence(self):
        # #168: raw observed MIME is a stored field; the essence is computed.
        assert "content_media_type" in WatchedItemResponse.model_fields
        assert "media_type_essence" in WatchedItemResponse.model_computed_fields
        assert "media_type_essence" not in WatchedItemResponse.model_fields
        assert "default_content_type" not in WatchedItemResponse.model_fields

    def test_computed_essence_is_the_resolved_dispatch_essence(self):
        """#168: media_type_essence reflects the *dispatch* resolution (tiebreaker),
        the same value the pipeline dispatches on — not just the header projection."""

        def _essence(content_media_type, url):
            wi = WatchedItem(
                archiver_info_source_id=str(ULID()), archiver_info_item_id=ULID(), name="X"
            )
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
        assert "health_status" in WatchedItemResponse.model_fields

    def test_response_has_last_checked_at(self):
        assert "last_checked_at" in WatchedItemResponse.model_fields

    def test_response_has_archiver_info_source_id(self):
        assert "archiver_info_source_id" in WatchedItemResponse.model_fields

    def test_create_accepts_archiver_info_source_id(self):
        schema = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
        )
        assert schema.archiver_info_source_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"

    def test_patch_accepts_archiver_info_source_id(self):
        p = WatchedItemPatch(archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00")
        assert p.archiver_info_source_id == "01ABCDEFGHJKMNPQRSTVWXYZ00"

    def test_patch_archiver_info_source_id_is_optional(self):
        p = WatchedItemPatch()
        assert "archiver_info_source_id" not in p.model_dump(exclude_unset=True)

    def test_create_rejects_empty_archiver_info_source_id(self):
        """#8: empty string must be rejected — semantically different from null."""
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                url="https://example.com",
                archiver_info_source_id="",
                source_specs=_SPECS,
            )

    def test_patch_rejects_empty_archiver_info_source_id(self):
        """#8: same constraint on the patch schema."""
        with pytest.raises(ValidationError):
            WatchedItemPatch(archiver_info_source_id="")


class TestIssue187SchemaAdditions:
    """#187 — WatchedItemPatch must expose effective_url and source_specs."""

    def test_patch_accepts_effective_url(self):
        p = WatchedItemPatch(effective_url="https://example.com/new")
        assert p.effective_url == "https://example.com/new"

    def test_patch_effective_url_is_optional(self):
        p = WatchedItemPatch()
        assert "effective_url" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_empty_effective_url(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(effective_url="")

    def test_patch_accepts_source_specs(self):
        specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        p = WatchedItemPatch(source_specs=specs)
        assert p.source_specs == specs

    def test_patch_source_specs_is_optional(self):
        p = WatchedItemPatch()
        assert "source_specs" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_explicit_null_effective_url(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(effective_url=None)

    def test_patch_rejects_explicit_null_source_specs(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(source_specs=None)

    def test_patch_rejects_explicit_null_name(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(name=None)


class TestIssue188IsActive:
    """#188 — is_active on WatchedItemCreate (default True) and WatchedItemPatch."""

    def test_create_defaults_is_active_true(self):
        c = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=_SPECS,
        )
        assert c.is_active is True

    def test_create_accepts_is_active_false(self):
        c = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            is_active=False,
            source_specs=_SPECS,
        )
        assert c.is_active is False

    def test_patch_accepts_is_active(self):
        p = WatchedItemPatch(is_active=False)
        assert p.is_active is False

    def test_patch_is_active_is_optional(self):
        p = WatchedItemPatch()
        assert "is_active" not in p.model_dump(exclude_unset=True)

    def test_patch_rejects_explicit_null_is_active(self):
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
        c = ItemNotificationTemplateCreate(
            title="Item Template",
            remote_channel_id="01HV0000000000000000000099",
        )
        assert c.events == ["change_detected"]
        assert c.channel_hint == "remote"

    def test_template_create_rejects_empty_channel_hint(self):
        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(
                title="Item Template",
                remote_channel_id="01HV0000000000000000000099",
                channel_hint="",
            )

    def test_template_create_requires_title(self):
        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(remote_channel_id="01HV0000000000000000000099")

    def test_template_create_requires_remote_channel_id(self):
        with pytest.raises(ValidationError):
            ItemNotificationTemplateCreate(title="Item Template")


class TestIssue260SourceSpecsRequired:
    """#260: `source_specs` is required and non-empty — the spec-less state is gone.

    Settled as options 3 + 2 together. Archiver, the only caller, always has
    specs in hand: its `registry_announcement` refuses to announce a source as
    live without non-empty `source_specs`, and `watcher_provisioning` always
    passes them. The "optional at create" affordance therefore named a state
    nobody could legitimately reach and the pipeline had no ratified behaviour
    for — it silently watched the whole page under a synthetic `[{}]`.
    """

    def test_create_requires_source_specs(self):
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                url="https://example.com",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            )

    def test_create_rejects_empty_source_specs(self):
        """An empty list is the same unreachable state spelled explicitly."""
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                url="https://example.com",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                source_specs=[],
            )

    def test_create_accepts_non_empty_source_specs(self):
        specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        c = WatchedItemCreate(
            archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            url="https://example.com",
            archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            source_specs=specs,
        )
        assert c.source_specs == specs

    def test_patch_rejects_empty_source_specs(self):
        """The create gate is worth nothing if PATCH can empty the list again."""
        with pytest.raises(ValidationError):
            WatchedItemPatch(source_specs=[])

    def test_openapi_advertises_source_specs_as_required(self):
        """A client generated from the spec must not send a body the API rejects."""
        schema = app.openapi()["components"]["schemas"]["WatchedItemCreate"]
        assert "source_specs" in schema["required"]
        assert schema["properties"]["source_specs"]["minItems"] == 1


class TestIssue251ULIDValidation:
    """#251 CR-2: the Archiver links are ULID references, validated as such.

    Both are required now and both are load-bearing downstream — the drain
    hands ``archiver_info_source_id`` straight to Archiver, and the create
    route calls ``ULID.from_str`` on the InfoItem id. A length check alone let
    a malformed value persist and fail much later, against a real captured
    revision.
    """

    @pytest.mark.parametrize(
        "bad",
        ["not-a-ulid", "x" * 26, "01abcdefghjkmnpqrstvwxyz00", "01ABCDEFGHJKMNPQRSTVWXYZ0"],
        ids=["too-short", "bad-alphabet", "lowercase", "off-by-one"],
    )
    def test_create_rejects_malformed_archiver_info_source_id(self, bad):
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                url="https://example.com",
                archiver_info_source_id=bad,
                source_specs=_SPECS,
            )

    def test_create_rejects_malformed_archiver_info_item_id(self):
        with pytest.raises(ValidationError):
            WatchedItemCreate(
                archiver_info_item_id="not-a-ulid",
                url="https://example.com",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                source_specs=_SPECS,
            )

    def test_patch_rejects_malformed_archiver_info_source_id(self):
        with pytest.raises(ValidationError):
            WatchedItemPatch(archiver_info_source_id="not-a-ulid")

    def test_openapi_advertises_the_ulid_format(self):
        """#251 CR-10: the spec must carry the constraint the server enforces.

        A BeforeValidator is invisible to JSON Schema, so a client generated
        happily send something the API rejects.
        """
        props = app.openapi()["components"]["schemas"]["WatchedItemCreate"]["properties"]
        for field in ("archiver_info_item_id", "archiver_info_source_id"):
            schema = props[field]
            assert schema.get("format") == "ulid", field
            assert schema.get("pattern") == r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", field

    @pytest.mark.parametrize(
        "value",
        [
            "01ABCDEFGHJKMNPQRSTVWXYZ00",
            "7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
            "01abcdefghjkmnpqrstvwxyz00",
            "01ILOU" + "0" * 20,
            "8" + "0" * 25,
            "not-a-ulid",
            "01ABCDEFGHJKMNPQRSTVWXYZ0",
            "",
        ],
        ids=[
            "canonical",
            "max",
            "lowercase",
            "excluded-letters",
            "first-char-too-high",
            "garbage",
            "too-short",
            "empty",
        ],
    )
    def test_advertised_pattern_agrees_with_the_parser(self, value):
        """The spec's pattern and ULID.from_str must accept the same strings —
        an advertised constraint looser or tighter than the enforced one is
        worse than none."""
        try:
            ULID.from_str(value)
            parses = True
        except (ValueError, TypeError):
            parses = False

        assert bool(re.match(ULID_PATTERN, value)) is parses, value

    @pytest.mark.parametrize("bad", [12345, ["01ABCDEFGHJKMNPQRSTVWXYZ00"], {}, True])
    def test_create_rejects_non_string_archiver_refs(self, bad):
        """#251 CR-14: a non-string ref fails as a type error, not as a length
        complaint about its stringification."""
        with pytest.raises(ValidationError) as exc_info:
            WatchedItemCreate(
                archiver_info_item_id=bad,
                url="https://example.com",
                archiver_info_source_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
                source_specs=_SPECS,
            )
        assert "type str" in str(exc_info.value)
