"""Authoring tool endpoints under /api/v1/tools/*.

Non-mutating helpers that an LLM agent (or human operator) calls while
composing Information Items + InfoSpecs. Mutating CRUD lives on the existing
/api/v1/info-items and /api/v1/info-items/{id}/info-specs routes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.api.deps import get_db_session, get_http_fetcher
from src.information.api.schemas.info_item import InfoItemOut
from src.information.api.schemas.tools import (
    ChunkPreviewOut,
    FetchAndRenderRequest,
    FetchAndRenderResult,
    PreviewExtractionRequest,
    PreviewExtractionResult,
    ProposeSelectorsRequest,
    SelectorCandidateOut,
    ValidateInfoSpecRequest,
    ValidateInfoSpecResult,
    ValidationIssueOut,
)
from src.information.api.serializers import info_item_to_out
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec_with_errors,
)
from src.information.core.tools.fetch_and_render import (
    HttpFetcherProtocol,
    fetch_and_render,
)
from src.information.core.tools.find_info_item import find_info_item
from src.information.core.tools.preview_extraction import (
    TargetUnreachableError,
    preview_extraction,
)
from src.information.core.tools.propose_selectors import propose_selectors

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
    return [info_item_to_out(item) for item in items]


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


@router.post("/preview-extraction", response_model=PreviewExtractionResult)
async def preview_extraction_route(
    body: PreviewExtractionRequest,
    fetcher: HttpFetcherProtocol = Depends(get_http_fetcher),
) -> PreviewExtractionResult:
    """Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    Composes ``validate_info_spec`` + ``fetch_and_render`` + the HTML extractor
    + the spec's fingerprint algorithm so an authoring agent can verify the
    spec yields the expected content before persisting via ``create_info_spec``
    or ``create_info_item(initial_info_spec=…)``.

    Returns 422 with structured errors on schema validation failure
    (``error: "validation_failed"``) or target unreachability
    (``error: "target_unreachable"``).
    """
    try:
        result = await preview_extraction(fetcher, str(body.url), body.document)
    except InfoSpecValidationError as e:
        # The exception carries the structured per-field issue list directly,
        # so we render the route's contract (list of {path, message}) without
        # re-running the validator.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_failed",
                "errors": [{"path": i.path, "message": i.message} for i in e.issues]
                or [{"path": [], "message": str(e)}],
            },
        ) from e
    except TargetUnreachableError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "target_unreachable", "message": str(e)},
        ) from e

    return PreviewExtractionResult(
        chunks=[
            ChunkPreviewOut(
                index=c.index,
                chunk_type=c.chunk_type,
                label=c.label,
                text=c.text,
                char_count=c.char_count,
            )
            for c in result.chunks
        ],
        total_chars=result.total_chars,
        fingerprint_algorithm=result.fingerprint_algorithm,
        computed_fingerprint=result.computed_fingerprint,
    )


@router.post("/propose-selectors", response_model=list[SelectorCandidateOut])
async def propose_selectors_route(
    body: ProposeSelectorsRequest,
    fetcher: HttpFetcherProtocol = Depends(get_http_fetcher),
) -> list[SelectorCandidateOut]:
    """Suggest CSS selector candidates for content matching ``description``.

    v1 returns CSS selectors only — pair with ``extraction.algorithm: "css"``
    in the resulting InfoSpec. XPath / JSONPath / regex / full_page proposers
    are on the roadmap; track via #148.

    Heuristic v1: substring match + specificity + text-length proximity +
    volatility penalty (hash-looking class names get demoted). Empty match
    set returns ``[]``. Operators always verify the chosen selector via
    ``preview_extraction`` before persisting an InfoSpec.
    """
    candidates = await propose_selectors(fetcher, str(body.url), body.description, top_k=body.top_k)
    return [
        SelectorCandidateOut(
            selector=c.selector,
            sample_text=c.sample_text,
            stability_score=c.stability_score,
        )
        for c in candidates
    ]
