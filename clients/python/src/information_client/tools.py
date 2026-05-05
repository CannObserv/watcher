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
from typing import Any

from information_client.errors import error_from_response
from information_client.generated.models.info_item_out import InfoItemOut


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


async def _post_json(client_facade, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Send a JSON POST through the generated client's httpx instance.

    Returns the parsed JSON body on 2xx; raises a typed ``InformationClientError``
    subclass otherwise (mirrors the ``_unwrap`` helper used by generated calls).
    """
    httpx_client = client_facade._gen_client.get_async_httpx_client()
    response = await httpx_client.post(path, json=body)
    if 200 <= response.status_code < 300:
        return response.json()
    raise error_from_response(int(response.status_code), response.content)


async def _get_json(client_facade, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET counterpart of ``_post_json``."""
    httpx_client = client_facade._gen_client.get_async_httpx_client()
    response = await httpx_client.get(path, params=params or {})
    if 200 <= response.status_code < 300:
        return response.json()
    raise error_from_response(int(response.status_code), response.content)


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
    client_facade,
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


async def find_info_item(client_facade, query: str, *, limit: int = 20) -> list[Any]:
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


async def validate_info_spec(client_facade, document: dict[str, Any]) -> ValidationResult:
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
