"""Pydantic request/response schemas for /api/v1/tools/* endpoints."""

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class ValidateInfoSpecRequest(BaseModel):
    """Request body for POST /api/v1/tools/validate-info-spec."""

    document: dict[str, Any] = Field(
        description="The InfoSpec document to validate against the v1 JSON Schema."
    )


class ValidationIssueOut(BaseModel):
    """Single validation problem with a structured path + message."""

    path: list[str | int] = Field(
        description="JSON path to the offending field, as a list of segments."
    )
    message: str = Field(description="Human-readable error message from the validator.")


class ValidateInfoSpecResult(BaseModel):
    """Response body for POST /api/v1/tools/validate-info-spec."""

    valid: bool = Field(description="True iff the document passed schema validation.")
    errors: list[ValidationIssueOut] = Field(
        default_factory=list,
        description="Per-field validation issues; empty when ``valid`` is True.",
    )


class FetchAndRenderRequest(BaseModel):
    """Request body for POST /api/v1/tools/fetch-and-render."""

    url: HttpUrl = Field(description="Target URL to fetch (http/https only).")
    render: bool = Field(
        default=False,
        description=(
            "If True, render the page via Playwright before returning. v1 returns "
            "501 — wired in once the Playwright fetcher (#3) lands."
        ),
    )


class FetchAndRenderResult(BaseModel):
    """Response body for POST /api/v1/tools/fetch-and-render."""

    url: str = Field(description="Echo of the requested URL.")
    status_code: int = Field(description="HTTP status code from the target.")
    headers: dict[str, str] = Field(description="Response headers from the target.")
    body: str = Field(
        description=(
            "Decoded response body, truncated at 5 MiB. ``truncated`` is True when "
            "the original payload exceeded the cap."
        )
    )
    body_bytes_total: int = Field(
        description="Original byte count before any truncation; useful for size sanity checks."
    )
    truncated: bool = Field(description="True when ``body`` was truncated to the 5 MiB cap.")
    screenshot_url: str | None = Field(
        default=None,
        description=(
            "Reserved for the Playwright fetcher path; always None in v1 since "
            "screenshot capture isn't wired."
        ),
    )
