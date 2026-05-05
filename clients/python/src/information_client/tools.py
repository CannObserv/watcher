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
