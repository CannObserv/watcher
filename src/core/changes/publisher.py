"""ChangePublisher — concrete Redis Streams implementation.

No abstraction layer; if a future broker migration is needed, refactor
at that point with knowledge of operational constraints.

Wire format (per stream entry):
    field "key"                 = partition key (UTF-8 bytes)
    field "payload"             = opaque payload (bytes)
    field "hdr.<header_name>"   = header value (UTF-8 bytes), one field per header

Consumers should ignore unknown `hdr.*` fields — header set is open-ended.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio

from src.core.changes.redis_url import get_redis_url
from src.core.logging import get_logger

logger = get_logger(__name__)


class ChangePublisher:
    """Async publisher of Change records to Redis Streams.

    Construct with an explicit `redis_client` (recommended for tests via
    fakeredis), or with no args to lazily build one from `REDIS_URL`.
    """

    def __init__(self, *, redis_client: redis_asyncio.Redis | None = None) -> None:
        self._client = redis_client
        self._owns_client = redis_client is None

    async def _get_client(self) -> redis_asyncio.Redis:
        if self._client is None:
            self._client = redis_asyncio.from_url(get_redis_url())
        return self._client

    async def publish_change(
        self,
        topic: str,
        key: str,
        payload: bytes,
        headers: dict[str, str],
    ) -> str:
        """Publish a Change to the named Redis Stream.

        Returns the Redis Stream message ID (e.g. ``"1700000000000-0"``).
        """
        client = await self._get_client()
        fields: dict[str | bytes, str | bytes] = {
            "key": key.encode("utf-8"),
            "payload": payload,
        }
        for hdr_name, hdr_value in headers.items():
            fields[f"hdr.{hdr_name}"] = hdr_value.encode("utf-8")
        msg_id_bytes = await client.xadd(topic, fields)
        msg_id = (
            msg_id_bytes.decode("utf-8") if isinstance(msg_id_bytes, bytes) else str(msg_id_bytes)
        )
        logger.info(
            "change published",
            extra={"topic": topic, "key": key, "msg_id": msg_id, "payload_bytes": len(payload)},
        )
        return msg_id

    async def aclose(self) -> None:
        """Close the underlying Redis client if we own it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
