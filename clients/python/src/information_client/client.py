"""``InformationClient`` — public facade.

Wraps a single httpx.AsyncClient with X-API-Key auth and exposes ergonomic
methods that dispatch to the generated openapi-python-client output for
typed request/response handling.

The generated package's naming is verbose (FastAPI default operation IDs),
so callers should use this wrapper instead of importing from
``information_client.generated`` directly.

Async-only. No sync facade.
"""

from __future__ import annotations

import time
from types import TracebackType

import httpx

from information_client import tools as _tools
from information_client.errors import error_from_response
from information_client.generated.api.info_items import (
    create_info_item_api_v1_info_items_post,
    get_info_item_api_v1_info_items_info_item_id_get,
    list_info_items_api_v1_info_items_get,
)
from information_client.generated.api.info_specs import (
    create_info_spec_api_v1_info_items_info_item_id_info_specs_post,
    list_info_specs_api_v1_info_items_info_item_id_info_specs_get,
)
from information_client.generated.api.info_specs import (
    get_primary_info_spec_api_v1_info_items_info_item_id_primary_info_spec_get as _get_primary_spec,
)
from information_client.generated.api.info_specs import (
    patch_info_spec_api_v1_info_items_info_item_id_info_specs_info_spec_id_patch as _patch_spec,
)
from information_client.generated.client import AuthenticatedClient
from information_client.generated.models.info_item_create import InfoItemCreate
from information_client.generated.models.info_item_out import InfoItemOut
from information_client.generated.models.info_spec_create import InfoSpecCreate
from information_client.generated.models.info_spec_create_document import InfoSpecCreateDocument
from information_client.generated.models.info_spec_out import InfoSpecOut
from information_client.generated.models.info_spec_patch import InfoSpecPatch
from information_client.generated.types import UNSET

# Sentinels for omitting optional fields. The generated openapi-python-client
# uses `UNSET` from `generated.types` for unset attrs.
_UNSET_PRIORITY = UNSET  # type: ignore[assignment]
_UNSET_ACTIVE = UNSET  # type: ignore[assignment]


class InformationClient:
    """Async client for the Information service."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._mask = (api_key[:3] + "***") if api_key else "***"
        self._cache_ttl_seconds = cache_ttl_seconds
        self._primary_cache: dict[str, tuple[InfoSpecOut, float]] = {}
        self._gen_client = AuthenticatedClient(
            base_url=self._base_url,
            token=api_key,
            auth_header_name="X-API-Key",
            prefix="",
            timeout=httpx.Timeout(timeout),
            raise_on_unexpected_status=False,
        )

    def __repr__(self) -> str:
        return f"InformationClient(base_url={self._base_url!r}, api_key={self._mask!r})"

    async def __aenter__(self) -> InformationClient:
        await self._gen_client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._gen_client.__aexit__(exc_type, exc, tb)

    async def aclose(self) -> None:
        """Close the underlying generated client (mirrors ``async with`` exit)."""
        await self._gen_client.__aexit__(None, None, None)

    # --- InfoItem endpoints ---

    async def create_info_item(
        self, *, name: str, description: str | None = None, owner: str | None = None
    ) -> InfoItemOut:
        """Create a new InfoItem."""
        body = InfoItemCreate(name=name, description=description, owner=owner)
        response = await create_info_item_api_v1_info_items_post.asyncio_detailed(
            client=self._gen_client, body=body
        )
        return _unwrap(response)

    async def list_info_items(self) -> list[InfoItemOut]:
        """List all InfoItems."""
        response = await list_info_items_api_v1_info_items_get.asyncio_detailed(
            client=self._gen_client
        )
        return _unwrap(response)

    async def get_info_item(self, info_item_id: str) -> InfoItemOut:
        """Fetch a single InfoItem by ID."""
        response = await get_info_item_api_v1_info_items_info_item_id_get.asyncio_detailed(
            client=self._gen_client, info_item_id=info_item_id
        )
        return _unwrap(response)

    # --- InfoSpec endpoints ---

    async def get_primary_info_spec(
        self, info_item_id: str, *, force_refresh: bool = False
    ) -> InfoSpecOut:
        """Resolve the primary (lowest active priority) InfoSpec for an InfoItem.

        Hot path for consumer services (Watcher, Archive). Raises NotFound
        if the InfoItem doesn't exist or has no active InfoSpec.

        Results are cached per ``info_item_id`` for ``cache_ttl_seconds``
        (default 60 s). Pass ``force_refresh=True`` to bypass the cache and
        re-fetch from the service. Errors are never cached. Use
        ``invalidate_primary_cache()`` to evict entries on demand.
        """
        if not force_refresh:
            cached = self._primary_cache.get(info_item_id)
            if cached is not None:
                spec, expiry = cached
                if time.monotonic() < expiry:
                    return spec

        response = await _get_primary_spec.asyncio_detailed(
            client=self._gen_client, info_item_id=info_item_id
        )
        result = _unwrap(response)
        self._primary_cache[info_item_id] = (result, time.monotonic() + self._cache_ttl_seconds)
        return result

    def invalidate_primary_cache(self, info_item_id: str | None = None) -> None:
        """Evict cached primary InfoSpec entries.

        If ``info_item_id`` is given, evict only that entry.
        If ``info_item_id`` is None, clear the entire cache.
        """
        if info_item_id is None:
            self._primary_cache.clear()
        else:
            self._primary_cache.pop(info_item_id, None)

    async def list_active_info_specs(self, info_item_id: str) -> list[InfoSpecOut]:
        """Return all active InfoSpecs for an InfoItem, ordered by priority asc.

        Recovery path — call this only when the primary fails extraction.
        """
        response = (
            await list_info_specs_api_v1_info_items_info_item_id_info_specs_get.asyncio_detailed(
                client=self._gen_client, info_item_id=info_item_id
            )
        )
        return _unwrap(response)

    async def create_info_spec(
        self, info_item_id: str, *, document: dict, priority: int | None = None
    ) -> InfoSpecOut:
        """Create an InfoSpec for an InfoItem."""
        body = InfoSpecCreate(
            document=InfoSpecCreateDocument.from_dict(document),
            priority=priority if priority is not None else _UNSET_PRIORITY,
        )
        response = (
            await create_info_spec_api_v1_info_items_info_item_id_info_specs_post.asyncio_detailed(
                client=self._gen_client, info_item_id=info_item_id, body=body
            )
        )
        return _unwrap(response)

    async def patch_info_spec(
        self,
        info_item_id: str,
        info_spec_id: str,
        *,
        priority: int | None = None,
        active: bool | None = None,
    ) -> InfoSpecOut:
        """Patch an InfoSpec's priority or active status."""
        body = InfoSpecPatch(
            priority=priority if priority is not None else _UNSET_PRIORITY,
            active=active if active is not None else _UNSET_ACTIVE,
        )
        response = await _patch_spec.asyncio_detailed(
            client=self._gen_client,
            info_item_id=info_item_id,
            info_spec_id=info_spec_id,
            body=body,
        )
        return _unwrap(response)

    # --- Authoring tools (/api/v1/tools/*) ---

    async def validate_info_spec(self, document: dict) -> _tools.ValidationResult:
        """Validate an InfoSpec document against the v1 JSON Schema.

        Always returns a result. ``valid=False`` carries per-field issues; use
        this during authoring to surface schema problems without committing.
        """
        return await _tools.validate_info_spec(self, document)


def _unwrap(response):
    """Return parsed body on 2xx; raise typed error otherwise.

    ``response`` is a generated ``Response[T]``; ``response.parsed`` is the
    typed body produced by the openapi-python-client output. Return type is
    intentionally untyped at this boundary — callers' annotations carry the
    static type, since the parsed body's runtime type is determined by which
    generated endpoint produced ``response``.
    """
    if 200 <= response.status_code < 300:
        return response.parsed
    raise error_from_response(int(response.status_code), response.content)
