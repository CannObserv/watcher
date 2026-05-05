"""Pydantic request/response schemas for /api/v1/tools/* endpoints."""

from typing import Any

from pydantic import BaseModel, Field


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
