"""URL probe endpoint — resolve effective URL and domain."""

from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import get_probe_fn
from src.core.probe import ProbeResult

router = APIRouter(prefix="/probe", tags=["probe"])


class ProbeRequest(BaseModel):
    """Input schema for the probe endpoint."""

    url: str


class ProbeResponse(BaseModel):
    """Output schema for the probe endpoint."""

    effective_url: str
    effective_domain: str
    redirect_chain: list[str]
    status_code: int
    content_type: str | None


@router.post("", response_model=ProbeResponse)
async def probe_endpoint(
    data: ProbeRequest,
    probe_fn: Annotated[Callable[[str], Awaitable[ProbeResult]], Depends(get_probe_fn)],
) -> ProbeResponse:
    """Probe a URL: follow redirects, return effective URL and domain."""
    try:
        result: ProbeResult = await probe_fn(data.url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"URL unreachable: {exc}") from exc

    return ProbeResponse(
        effective_url=result.effective_url,
        effective_domain=result.effective_domain,
        redirect_chain=result.redirect_chain,
        status_code=result.status_code,
        content_type=result.content_type,
    )
