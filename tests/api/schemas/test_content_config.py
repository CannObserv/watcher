"""Tests for ContentOptions and ContentConfig schemas."""

import pytest
from pydantic import ValidationError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEventType


class TestContentOptions:
    def test_defaults_all_false(self):
        opts = ContentOptions()
        assert opts.include_temporal_context is False
        assert opts.include_domain is False
        assert opts.include_last_changed_at is False
        assert opts.include_tags is False
        assert opts.include_description is False

    def test_explicit_values(self):
        opts = ContentOptions(include_domain=True, include_tags=True)
        assert opts.include_domain is True
        assert opts.include_tags is True

    def test_template_strings_default_none(self):
        opts = ContentOptions()
        assert opts.title_template is None
        assert opts.body_template is None


class TestContentConfig:
    def test_defaults_empty(self):
        cfg = ContentConfig()
        assert cfg.default == ContentOptions()
        assert cfg.overrides == {}

    def test_override_valid_event_type(self):
        event_type = WatchEventType.CHANGE_DETECTED.value
        cfg = ContentConfig(
            default=ContentOptions(),
            overrides={event_type: ContentOptions(include_domain=True)},
        )
        assert cfg.overrides[event_type].include_domain is True

    def test_override_invalid_event_type_rejected(self):
        with pytest.raises(ValidationError):
            ContentConfig(overrides={"invalid_type": ContentOptions()})

    def test_override_multiple_invalid_event_types_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ContentConfig(
                overrides={
                    "invalid_type_1": ContentOptions(),
                    "invalid_type_2": ContentOptions(),
                }
            )
        # Verify the error message includes both invalid keys
        error_msg = str(exc_info.value)
        assert "invalid_type_1" in error_msg
        assert "invalid_type_2" in error_msg

    def test_roundtrip_json(self):
        event_type = WatchEventType.WATCH_ERROR.value
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={event_type: ContentOptions(include_temporal_context=True)},
        )
        data = cfg.model_dump()
        restored = ContentConfig.model_validate(data)
        assert restored.default.include_domain is True
        assert restored.overrides[event_type].include_temporal_context is True
