"""Shared dashboard form-parsing helpers."""

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEventType

ALL_EVENT_TYPE_VALUES: list[str] = [e.value for e in WatchEventType]


def _status_to_is_active(status: str | None) -> bool | None:
    """Convert status string param to is_active bool for DB queries."""
    if status == "active":
        return True
    if status == "inactive":
        return False
    return None


def parse_content_config_from_form(form) -> dict | None:
    """Extract content_config fields from a flat form POST dict."""
    title_template = form.get("content_config__title_template", "").strip() or None
    body_template = form.get("content_config__body_template", "").strip() or None
    opts = ContentOptions(
        include_temporal_context="content_config__include_temporal_context" in form,
        include_domain="content_config__include_domain" in form,
        include_last_changed_at="content_config__include_last_changed_at" in form,
        include_tags="content_config__include_tags" in form,
        include_description="content_config__include_description" in form,
        title_template=title_template,
        body_template=body_template,
    )
    # Only store if at least one toggle is enabled or a template string is provided.
    any_enabled = (
        opts.include_temporal_context
        or opts.include_domain
        or opts.include_last_changed_at
        or opts.include_tags
        or opts.include_description
        or opts.title_template
        or opts.body_template
    )
    # Parse per-event overrides
    overrides: dict[str, ContentOptions] = {}
    for et_value in ALL_EVENT_TYPE_VALUES:
        prefix = f"content_config__override__{et_value}__"
        et_opts = ContentOptions(
            include_temporal_context=f"{prefix}include_temporal_context" in form,
            include_domain=f"{prefix}include_domain" in form,
            include_last_changed_at=f"{prefix}include_last_changed_at" in form,
            include_tags=f"{prefix}include_tags" in form,
            include_description=f"{prefix}include_description" in form,
        )
        if any(
            x
            for x in (
                et_opts.include_temporal_context,
                et_opts.include_domain,
                et_opts.include_last_changed_at,
                et_opts.include_tags,
                et_opts.include_description,
            )
        ):
            overrides[et_value] = et_opts

    if not any_enabled and not overrides:
        return None
    return ContentConfig(default=opts, overrides=overrides).model_dump()
