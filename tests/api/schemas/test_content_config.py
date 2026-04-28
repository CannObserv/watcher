"""Tests for ContentOptions and ContentConfig schemas."""

import pytest
from pydantic import ValidationError

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEventType


class TestContentOptions:
    def test_defaults_all_false(self):
        opts = ContentOptions()
        assert opts.include_diff_snippet is False
        assert opts.include_diff_full is False
        assert opts.include_temporal_context is False
        assert opts.include_domain is False
        # Bumped from 10 → 25 in #116: unified diffs need more headroom than
        # the old chunk-label summary (each hunk header + ~3 context lines
        # ≈ 7 lines, so 10 was barely one hunk).
        assert opts.diff_snippet_lines == 25

    def test_explicit_values(self):
        opts = ContentOptions(include_diff_snippet=True, diff_snippet_lines=5)
        assert opts.include_diff_snippet is True
        assert opts.diff_snippet_lines == 5

    def test_diff_snippet_lines_must_be_positive(self):
        with pytest.raises(ValidationError):
            ContentOptions(diff_snippet_lines=0)

    def test_diff_snippet_lines_max(self):
        with pytest.raises(ValidationError):
            ContentOptions(diff_snippet_lines=201)

    def test_diff_snippet_lines_boundary_values(self):
        # Verify min and max boundary values are accepted
        opts_min = ContentOptions(diff_snippet_lines=1)
        assert opts_min.diff_snippet_lines == 1

        opts_max = ContentOptions(diff_snippet_lines=200)
        assert opts_max.diff_snippet_lines == 200


class TestContentConfig:
    def test_defaults_empty(self):
        cfg = ContentConfig()
        assert cfg.default == ContentOptions()
        assert cfg.overrides == {}

    def test_override_valid_event_type(self):
        event_type = WatchEventType.CHANGE_DETECTED.value
        cfg = ContentConfig(
            default=ContentOptions(),
            overrides={event_type: ContentOptions(include_diff_snippet=True)},
        )
        assert cfg.overrides[event_type].include_diff_snippet is True

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
