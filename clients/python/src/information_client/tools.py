"""Hand-written wrappers for /api/v1/tools/* endpoints.

Mixed into ``InformationClient`` via subclassing in ``client.py``. These
endpoints are reached via the generated client's underlying httpx instance
so we don't have to wait on a regen cycle for each new tool.

Once ``clients/python/regen.sh`` regenerates the generated package against
the live OpenAPI spec, these wrappers can be tightened to use the typed
generated bindings; until then, we work with plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from information_client.errors import error_from_response
from information_client.generated.models.info_item_out import InfoItemOut

if TYPE_CHECKING:
    from information_client.client import InformationClient


@dataclass(frozen=True)
class ValidationIssue:
    """One validation problem with a structured path + message."""

    path: list[str | int]
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a ``validate_info_spec`` call."""

    valid: bool
    errors: list[ValidationIssue]


async def _post_json(
    client_facade: InformationClient, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Send a JSON POST through the generated client's httpx instance.

    Returns the parsed JSON body on 2xx; raises a typed ``InformationClientError``
    subclass otherwise (mirrors the ``_unwrap`` helper used by generated calls).
    """
    httpx_client = client_facade._gen_client.get_async_httpx_client()
    response = await httpx_client.post(path, json=body)
    if 200 <= response.status_code < 300:
        return response.json()
    raise error_from_response(int(response.status_code), response.content)


async def _get_json(
    client_facade: InformationClient, path: str, params: dict[str, Any] | None = None
) -> Any:
    """GET counterpart of ``_post_json``."""
    httpx_client = client_facade._gen_client.get_async_httpx_client()
    response = await httpx_client.get(path, params=params or {})
    if 200 <= response.status_code < 300:
        return response.json()
    raise error_from_response(int(response.status_code), response.content)


@dataclass(frozen=True)
class FetchAndRenderResult:
    """Outcome of a ``fetch_and_render`` call."""

    url: str
    status_code: int
    headers: dict[str, str]
    body: str
    body_bytes_total: int
    truncated: bool
    screenshot_url: str | None


async def fetch_and_render(
    client_facade: InformationClient, url: str, *, render: bool = False
) -> FetchAndRenderResult:
    """Fetch ``url`` and return body + headers.

    ``render=True`` raises ``InformationClientError`` (501) until #3 lands.
    Body bytes larger than 5 MiB are truncated server-side; check
    ``truncated`` and ``body_bytes_total`` to detect.
    """
    body = await _post_json(
        client_facade,
        "/api/v1/tools/fetch-and-render",
        {"url": url, "render": render},
    )
    return FetchAndRenderResult(
        url=str(body["url"]),
        status_code=int(body["status_code"]),
        headers=dict(body.get("headers") or {}),
        body=str(body["body"]),
        body_bytes_total=int(body["body_bytes_total"]),
        truncated=bool(body["truncated"]),
        screenshot_url=body.get("screenshot_url"),
    )


@dataclass(frozen=True)
class SelectorCandidate:
    """One ranked selector candidate from ``propose_selectors``."""

    selector: str
    sample_text: str
    stability_score: float


async def propose_selectors(
    client_facade: InformationClient,
    url: str,
    description: str,
    *,
    top_k: int = 5,
) -> list[SelectorCandidate]:
    """Return ranked selector candidates for ``description`` on ``url``.

    Empty match set returns ``[]``. Always pair with ``preview_extraction``
    against the chosen candidate before persisting an InfoSpec — the ranker
    is heuristic and meant to narrow the search space, not to finalise.
    """
    body = await _post_json(
        client_facade,
        "/api/v1/tools/propose-selectors",
        {"url": url, "description": description, "top_k": top_k},
    )
    return [
        SelectorCandidate(
            selector=str(c["selector"]),
            sample_text=str(c["sample_text"]),
            stability_score=float(c["stability_score"]),
        )
        for c in body
    ]


@dataclass(frozen=True)
class ChunkPreview:
    """Per-chunk preview entry from ``preview_extraction``."""

    index: int
    chunk_type: str
    label: str
    text: str
    char_count: int


@dataclass(frozen=True)
class PreviewExtractionResult:
    """Outcome of a ``preview_extraction`` call."""

    chunks: list[ChunkPreview]
    total_chars: int
    fingerprint_algorithm: str
    computed_fingerprint: str


async def preview_extraction(
    client_facade: InformationClient,
    url: str,
    document: dict[str, Any],
) -> PreviewExtractionResult:
    """Validate, fetch, extract, and fingerprint with a candidate InfoSpec.

    On schema validation failure or target unreachability, the underlying
    HTTPException is surfaced as an ``InformationClientError`` subclass with
    the structured ``detail`` body intact.
    """
    body = await _post_json(
        client_facade,
        "/api/v1/tools/preview-extraction",
        {"url": url, "document": document},
    )
    return PreviewExtractionResult(
        chunks=[
            ChunkPreview(
                index=int(c["index"]),
                chunk_type=str(c["chunk_type"]),
                label=str(c["label"]),
                text=str(c["text"]),
                char_count=int(c["char_count"]),
            )
            for c in body.get("chunks", [])
        ],
        total_chars=int(body["total_chars"]),
        fingerprint_algorithm=str(body["fingerprint_algorithm"]),
        computed_fingerprint=str(body["computed_fingerprint"]),
    )


@dataclass(frozen=True)
class InfoItemWithSpecResult:
    """``create_info_item`` response when ``initial_info_spec`` was supplied.

    Carries both the new ``info_item_id`` and the freshly-created
    ``info_spec_id`` so the caller can immediately reference the primary spec
    without a second round-trip.
    """

    info_item_id: str
    info_spec_id: str
    name: str
    description: str | None
    owner: str | None


async def create_info_item_atomic(
    client_facade: InformationClient,
    *,
    name: str,
    description: str | None = None,
    owner: str | None = None,
    initial_info_spec: dict[str, Any],
) -> InfoItemWithSpecResult:
    """Create an InfoItem and its primary InfoSpec in one transaction.

    Use this when you've already authored both the InfoItem metadata and the
    initial InfoSpec document (e.g. via ``validate_info_spec`` /
    ``preview_extraction``). On schema validation failure, neither row is
    persisted; an ``InformationClientError`` subclass is raised instead.
    """
    body: dict[str, Any] = {"name": name, "initial_info_spec": initial_info_spec}
    if description is not None:
        body["description"] = description
    if owner is not None:
        body["owner"] = owner
    payload = await _post_json(client_facade, "/api/v1/info-items", body)
    return InfoItemWithSpecResult(
        info_item_id=str(payload["info_item_id"]),
        info_spec_id=str(payload["info_spec_id"]),
        name=str(payload["name"]),
        description=payload.get("description"),
        owner=payload.get("owner"),
    )


async def find_info_item(
    client_facade: InformationClient, query: str, *, limit: int = 20
) -> list[InfoItemOut]:
    """Search Information Items by name + description (case-insensitive substring).

    Returns a list of ``InfoItemOut`` instances (the generated model) so callers
    get the same typed shape as ``list_info_items``. Use before ``create_info_item``
    to dedupe against existing entries.
    """
    body = await _get_json(
        client_facade,
        "/api/v1/tools/find-info-items",
        params={"q": query, "limit": limit},
    )
    return [InfoItemOut.from_dict(item) for item in body]


async def validate_info_spec(
    client_facade: InformationClient, document: dict[str, Any]
) -> ValidationResult:
    """Validate an InfoSpec document against the v1 JSON Schema.

    Always returns a result; ``valid=False`` carries per-field issues. Use this
    while authoring an InfoSpec to see what's wrong before calling
    ``create_info_spec`` (which would otherwise return 422).
    """
    body = await _post_json(
        client_facade, "/api/v1/tools/validate-info-spec", {"document": document}
    )
    return ValidationResult(
        valid=bool(body["valid"]),
        errors=[
            ValidationIssue(path=list(e["path"]), message=str(e["message"]))
            for e in body.get("errors", [])
        ],
    )
