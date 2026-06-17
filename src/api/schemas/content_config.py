"""Pydantic schemas for notification content configuration."""

from pydantic import BaseModel, Field, field_validator

from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


class ContentOptions(BaseModel):
    """Field toggles controlling what extra information appears in a notification body."""

    include_diff_snippet: bool = False
    """Include the first `diff_snippet_lines` lines of the unified diff in the body."""

    diff_snippet_lines: int = Field(default=25, ge=1, le=200)
    """Max number of unified-diff lines to include in snippet mode.
    Truncation is hunk-boundary aware — never mid-hunk."""

    include_diff_full: bool = False
    """Include the full unified diff. Supersedes include_diff_snippet if both set."""

    include_temporal_context: bool = False
    """Include check interval in the body."""

    include_domain: bool = False
    """Include the effective domain in the body."""

    include_last_changed_at: bool = False
    """Include the date the watch last detected a change in the body."""

    include_significance: bool = False
    """Include the change significance score as a percentage in the body."""

    include_change_dashboard_url: bool = False
    """Include a direct link to the change detail page in the dashboard."""

    include_tags: bool = False
    """Include the watch's tags list in the body."""

    include_description: bool = False
    """Include the watch's description in the body."""

    title_template: str | None = None
    """Jinja2 template string for the notification title. Overrides the default title when set.
    Context: watched_item_id, item_name, item_url, event_type, occurred_at, plus all metadata
    keys."""

    body_template: str | None = None
    """Jinja2 template string for the notification body. Overrides build_body() output when set.
    Context: same as title_template."""


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
