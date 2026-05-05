"""Authoring tool endpoints under /api/v1/tools/*.

Non-mutating helpers that an LLM agent (or human operator) calls while
composing Information Items + InfoSpecs. Mutating CRUD lives on the existing
/api/v1/info-items and /api/v1/info-items/{id}/info-specs routes.
"""

from fastapi import APIRouter

from src.information.api.schemas.tools import (
    ValidateInfoSpecRequest,
    ValidateInfoSpecResult,
    ValidationIssueOut,
)
from src.information.core.info_spec_schema import validate_info_spec_with_errors

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("/validate-info-spec", response_model=ValidateInfoSpecResult)
async def validate_info_spec_route(body: ValidateInfoSpecRequest) -> ValidateInfoSpecResult:
    """Validate an InfoSpec document against the v1 JSON Schema.

    Always returns 200 — the response body's ``valid`` flag carries the
    validation outcome, and ``errors`` carries field-level issues. This
    differs from create/patch routes (which return 422 on invalid input);
    here, validation IS the purpose, so the result is the response.
    """
    issues = validate_info_spec_with_errors(body.document)
    return ValidateInfoSpecResult(
        valid=len(issues) == 0,
        errors=[ValidationIssueOut(path=i.path, message=i.message) for i in issues],
    )
