"""Pydantic schemas for notification content configuration.

Phase 1 fields: diff snippet/full, temporal context, domain.
include_tags and include_description from the design doc were deferred — Watch does not
have tags or description fields yet. Add them here when those Watch fields exist.
"""

from pydantic import BaseModel, Field, field_validator

from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


class ContentOptions(BaseModel):
    """Field toggles controlling what extra information appears in a notification body."""

    include_diff_snippet: bool = False
    """Include the first `diff_snippet_lines` changed sections in the body."""

    diff_snippet_lines: int = Field(default=10, ge=1, le=100)
    """Max number of changed-section entries to include in snippet mode."""

    include_diff_full: bool = False
    """Include all changed sections. Supersedes include_diff_snippet if both set."""

    include_temporal_context: bool = False
    """Include check interval in the body."""

    include_domain: bool = False
    """Include the effective domain in the body."""

    include_last_changed_at: bool = False
    """Include the date the watch last detected a change in the body."""

    include_significance: bool = False
    """Include the change significance score as a percentage in the body."""


class ContentConfig(BaseModel):
    """Per-config content customisation: default options with optional per-event overrides."""

    default: ContentOptions = Field(default_factory=ContentOptions)
    """Applied to all events unless an override exists for the specific event type."""

    overrides: dict[str, ContentOptions] = Field(default_factory=dict)
    """event_type value → ContentOptions. Keys must be valid WatchEventType values.

    Priority between include_diff_snippet and include_diff_full is handled by the content builder.
    """

    @field_validator("overrides")
    @classmethod
    def validate_override_keys(cls, v: dict[str, ContentOptions]) -> dict[str, ContentOptions]:
        invalid = [k for k in v if k not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s) in overrides: {invalid}. "
                f"Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v
