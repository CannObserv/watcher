"""Authoring tool endpoints under /api/v1/tools/*.

Non-mutating helpers that an LLM agent (or human operator) calls while
composing Information Items + InfoSpecs. Mutating CRUD lives on the existing
/api/v1/info-items and /api/v1/info-items/{id}/info-specs routes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session, get_http_fetcher
from src.information.api.routes.info_items import _to_out as _info_item_to_out
from src.information.api.schemas.info_item import InfoItemOut
from src.information.api.schemas.tools import (
    FetchAndRenderRequest,
    FetchAndRenderResult,
    ValidateInfoSpecRequest,
    ValidateInfoSpecResult,
    ValidationIssueOut,
)
from src.information.core.info_spec_schema import validate_info_spec_with_errors
from src.information.core.tools.fetch_and_render import (
    HttpFetcherProtocol,
    fetch_and_render,
)
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


@router.post("/fetch-and-render", response_model=FetchAndRenderResult)
async def fetch_and_render_route(
    body: FetchAndRenderRequest,
    fetcher: HttpFetcherProtocol = Depends(get_http_fetcher),
) -> FetchAndRenderResult:
    """Fetch a target URL and return its body + headers for downstream tools.

    Use during InfoSpec authoring to inspect what the extractor will see (e.g.
    pipe the body into ``propose_selectors`` or ``preview_extraction``). Body
    payloads larger than 5 MiB are truncated; ``truncated`` flags the case.
    ``render=True`` returns 501 until the Playwright fetcher (#3) lands.
    """
    if body.render:
        raise HTTPException(status_code=501, detail="Playwright fetcher not yet integrated (#3)")
    result = await fetch_and_render(fetcher, str(body.url), render=False)
    return FetchAndRenderResult(
        url=result.url,
        status_code=result.status_code,
        headers=result.headers,
        body=result.body,
        body_bytes_total=result.body_bytes_total,
        truncated=result.truncated,
        screenshot_url=result.screenshot_url,
    )
