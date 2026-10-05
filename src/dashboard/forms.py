"""Shared dashboard form-parsing helpers."""

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEventType

ALL_EVENT_TYPE_VALUES: list[str] = [e.value for e in WatchEventType]

_DEFAULT_SNIPPET_LINES: int = ContentOptions.model_fields["diff_snippet_lines"].default


def _snippet_lines(form, field: str) -> int:
    """The snippet cap from its number input: clamped to the schema's bounds,
    the default when absent or unparseable — a form must never 500 on it."""
    try:
        lines = int(form.get(field, _DEFAULT_SNIPPET_LINES))
    except (ValueError, TypeError):
        return _DEFAULT_SNIPPET_LINES
    return max(1, min(200, lines))


def parse_content_config_from_form(form) -> dict | None:
    """Extract content_config fields from a flat form POST dict.

    The default card is stored only when it differs from ``ContentOptions()``.
    Not "when a toggle is on": the diff snippet is on by default (#222), and an
    unchecked checkbox is simply absent, so storing nothing for an unchecked
    snippet would hand the recipient the default — the snippet — back.
    """
    title_template = form.get("content_config__title_template", "").strip() or None
    body_template = form.get("content_config__body_template", "").strip() or None
    opts = ContentOptions(
        include_diff_snippet="content_config__include_diff_snippet" in form,
        diff_snippet_lines=_snippet_lines(form, "content_config__diff_snippet_lines"),
        include_diff_full="content_config__include_diff_full" in form,
        include_temporal_context="content_config__include_temporal_context" in form,
        include_domain="content_config__include_domain" in form,
        include_last_changed_at="content_config__include_last_changed_at" in form,
        include_tags="content_config__include_tags" in form,
        include_description="content_config__include_description" in form,
        title_template=title_template,
        body_template=body_template,
    )
    # Parse per-event overrides
    overrides: dict[str, ContentOptions] = {}
    for et_value in ALL_EVENT_TYPE_VALUES:
        prefix = f"content_config__override__{et_value}__"
        et_opts = ContentOptions(
            include_diff_snippet=f"{prefix}include_diff_snippet" in form,
            diff_snippet_lines=_snippet_lines(form, f"{prefix}diff_snippet_lines"),
            include_diff_full=f"{prefix}include_diff_full" in form,
            include_temporal_context=f"{prefix}include_temporal_context" in form,
            include_domain=f"{prefix}include_domain" in form,
            include_last_changed_at=f"{prefix}include_last_changed_at" in form,
            include_tags=f"{prefix}include_tags" in form,
            include_description=f"{prefix}include_description" in form,
        )
        if any(
            x
            for x in (
                et_opts.include_diff_snippet,
                et_opts.include_diff_full,
                et_opts.include_temporal_context,
                et_opts.include_domain,
                et_opts.include_last_changed_at,
                et_opts.include_tags,
                et_opts.include_description,
            )
        ):
            overrides[et_value] = et_opts

    if opts == ContentOptions() and not overrides:
        return None
    return ContentConfig(default=opts, overrides=overrides).model_dump()
