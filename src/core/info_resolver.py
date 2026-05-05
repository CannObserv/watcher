"""Resolve the primary InfoSpec for a Watch via the Information SDK.

The pipeline calls `resolve_primary(client, info_item_id)` to get a
plain-dict view of the primary spec's document, with optional `force_refresh`
to bypass the SDK's TTL cache (used after extraction failure to pick up
operator-side spec edits without waiting for cache expiry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from information_client import InformationClient


@dataclass(frozen=True)
class ResolvedInfoSpec:
    """Plain-dict view of a primary InfoSpec, ready for the pipeline."""

    info_item_id: str
    info_spec_id: str
    document: dict[str, Any]


def _document_to_dict(doc: Any) -> dict[str, Any]:
    """Coerce the SDK's InfoSpecOutDocument wrapper to a plain dict.

    The openapi-python-client wrapper exposes both `to_dict()` and
    `additional_properties` (verified at SDK generation time). Either
    works; we prefer `to_dict()` because it's the documented API.
    """
    if hasattr(doc, "to_dict"):
        return dict(doc.to_dict())
    if hasattr(doc, "additional_properties"):
        return dict(doc.additional_properties)
    return dict(doc)


async def resolve_primary(
    client: InformationClient,
    info_item_id: str,
    *,
    force_refresh: bool = False,
) -> ResolvedInfoSpec:
    """Resolve the primary InfoSpec for `info_item_id`.

    `force_refresh=True` bypasses the SDK's TTL cache and re-fetches from
    the Information service. The SDK overwrites its cache with the fresh
    value (no separate invalidate call required).

    Raises whatever the SDK raises (NotFound, ServerError, AuthError, etc.)
    — translation to retry/skip behavior is the caller's concern.
    """
    spec = await client.get_primary_info_spec(info_item_id, force_refresh=force_refresh)
    return ResolvedInfoSpec(
        info_item_id=str(spec.info_item_id),
        info_spec_id=str(spec.info_spec_id),
        document=_document_to_dict(spec.document),
    )
