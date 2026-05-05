"""Authoring tool endpoints under /api/v1/tools/*.

Non-mutating helpers that an LLM agent (or human operator) calls while
composing Information Items + InfoSpecs. Mutating CRUD lives on the existing
/api/v1/info-items and /api/v1/info-items/{id}/info-specs routes.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session
from src.information.api.routes.info_items import _to_out as _info_item_to_out
from src.information.api.schemas.info_item import InfoItemOut
from src.information.api.schemas.tools import (
    ValidateInfoSpecRequest,
    ValidateInfoSpecResult,
    ValidationIssueOut,
)
from src.information.core.info_spec_schema import validate_info_spec_with_errors
from src.information.core.tools.find_info_item import find_info_item

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


@router.get("/find-info-items", response_model=list[InfoItemOut])
async def find_info_items_route(
    q: str = Query(
        min_length=1,
        description="Substring matched against name + description (case-insensitive).",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum matches to return."),
    session: AsyncSession = Depends(get_db_session),
) -> list[InfoItemOut]:
    """Search Information Items by name or description (substring, case-insensitive).

    Use this *before* ``create_info_item`` to avoid duplicating an existing
    Information Item. Returns up to ``limit`` matches, newest first.
    """
    items = await find_info_item(session, q, limit=limit)
    return [_info_item_to_out(item) for item in items]
