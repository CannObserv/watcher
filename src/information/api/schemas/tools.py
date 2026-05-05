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


class PreviewExtractionRequest(BaseModel):
    """Request body for POST /api/v1/tools/preview-extraction."""

    url: HttpUrl = Field(description="Target URL to fetch and extract from.")
    document: dict[str, Any] = Field(
        description=(
            "Candidate InfoSpec document. Validated against the v1 schema before "
            "any fetch is attempted; a validation failure returns 422 with the "
            "per-field issue list and no fetch is performed."
        )
    )


class ChunkPreviewOut(BaseModel):
    """One chunk in the preview response."""

    index: int = Field(description="Position of the chunk in extraction order.")
    chunk_type: str = Field(description="Algorithm-specific type tag (e.g. 'page', 'section').")
    label: str = Field(description="Operator-readable chunk identifier.")
    text: str = Field(description="Extracted chunk text.")
    char_count: int = Field(description="Character count of ``text``.")


class PreviewExtractionResult(BaseModel):
    """Response body for POST /api/v1/tools/preview-extraction."""

    chunks: list[ChunkPreviewOut] = Field(
        description="Extracted chunks in order; empty when extraction yields nothing."
    )
    total_chars: int = Field(description="Sum of ``char_count`` across all chunks.")
    fingerprint_algorithm: str = Field(
        description="Algorithm used for ``computed_fingerprint`` (mirrors the spec)."
    )
    computed_fingerprint: str = Field(
        description=(
            "Fingerprint of the joined extracted text under the spec's algorithm. "
            "sha256 → 64-char hex; simhash → decimal int as a string."
        )
    )


class ProposeSelectorsRequest(BaseModel):
    """Request body for POST /api/v1/tools/propose-selectors."""

    url: HttpUrl = Field(description="Target URL to fetch and search.")
    description: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Plain-language description of the content the operator wants to "
            "extract. Matched against element text via case-insensitive "
            "substring search."
        ),
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=25,
        description="Maximum candidates to return; ranked by stability score (highest first).",
    )


class SelectorCandidateOut(BaseModel):
    """One ranked selector candidate."""

    selector: str = Field(description="CSS selector for the proposed element.")
    sample_text: str = Field(
        description="Visible text from the matched element (truncated to 200 chars)."
    )
    stability_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Heuristic score in [0, 1]: higher == more stable. Combines id/class "
            "structure, text-length proximity to the description, and a volatility "
            "penalty for hash-looking class names."
        ),
    )
