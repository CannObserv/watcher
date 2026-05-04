"""Reference consumer for `info.changes` — XREADGROUP loop with JSONL output.

Usage:
    uv run python tools/info_changes_consumer.py \
        --group archive-ref --output /tmp/info-changes.jsonl

Run alongside Watcher to verify the wire end-to-end. Acks each message after
writing it to the output file. On startup, creates the consumer group if it
doesn't exist (MKSTREAM ensures the stream exists).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as redis

from src.core.changes.redis_url import get_redis_url

DEFAULT_TOPIC = "info.changes"
DEFAULT_GROUP = "reference-consumer"


def _redact_url(url: str) -> str:
    """Strip userinfo from a URL so credentials don't leak into error output."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — re-bracket so urlunsplit produces a valid URL
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit(parts._replace(netloc=netloc))


def _safe_exc_message(e: BaseException) -> str:
    """Render an exception without trusting its repr — defense against creds in args."""
    args0 = e.args[0] if e.args else ""
    return f"{type(e).__name__}: {args0}"


async def _ensure_group(client: redis.Redis, topic: str, group: str) -> None:
    try:
        await client.xgroup_create(name=topic, groupname=group, id="$", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def consume(
    *,
    topic: str,
    group: str,
    consumer_name: str,
    output: Path,
    block_ms: int = 5000,
    max_messages: int | None = None,
) -> int:
    """Run the consume loop. Returns count of messages processed."""
    redis_url = get_redis_url()
    safe_url = _redact_url(redis_url)
    client = redis.from_url(redis_url)
    try:
        await _ensure_group(client, topic, group)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        await client.aclose()
        raise SystemExit(
            f"Redis unreachable at {safe_url}: {_safe_exc_message(e)}. "
            "Is redis-server running? (sudo systemctl status redis-server)"
        ) from e
    processed = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("a", encoding="utf-8") as fp:
            while True:
                if max_messages is not None and processed >= max_messages:
                    break
                try:
                    response = await client.xreadgroup(
                        groupname=group,
                        consumername=consumer_name,
                        streams={topic: ">"},
                        count=10,
                        block=block_ms,
                    )
                except (redis.ConnectionError, redis.TimeoutError) as e:
                    raise SystemExit(
                        f"Redis connection lost at {safe_url}: {_safe_exc_message(e)}. "
                        "Is redis-server running? (sudo systemctl status redis-server)"
                    ) from e
                if not response:
                    continue
                for _stream, entries in response:
                    for msg_id, fields in entries:
                        record = _format(msg_id, fields)
                        fp.write(json.dumps(record) + "\n")
                        fp.flush()
                        try:
                            await client.xack(topic, group, msg_id)
                        except (redis.ConnectionError, redis.TimeoutError) as e:
                            raise SystemExit(
                                f"Redis connection lost at {safe_url}: {_safe_exc_message(e)}. "
                                "Is redis-server running? (sudo systemctl status redis-server)"
                            ) from e
                        processed += 1
                        if max_messages is not None and processed >= max_messages:
                            break
                    if max_messages is not None and processed >= max_messages:
                        break
    finally:
        await client.aclose()
    return processed


def _format(msg_id: bytes, fields: dict[bytes, bytes]) -> dict:
    out: dict = {"_msg_id": msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)}
    if b"key" in fields:
        out["key"] = fields[b"key"].decode("utf-8")
    if b"payload" in fields:
        try:
            out["payload"] = json.loads(fields[b"payload"].decode("utf-8"))
        except json.JSONDecodeError:
            out["payload"] = fields[b"payload"].decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for k, v in fields.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        if key.startswith("hdr."):
            headers[key[4:]] = v.decode("utf-8") if isinstance(v, bytes) else str(v)
    if headers:
        out["headers"] = headers
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reference consumer for info.changes")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--consumer", default="ref-1")
    parser.add_argument("--output", type=Path, default=Path("/tmp/info-changes.jsonl"))
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Exit after this many messages (default: run forever)",
    )
    args = parser.parse_args()
    processed = asyncio.run(
        consume(
            topic=args.topic,
            group=args.group,
            consumer_name=args.consumer,
            output=args.output,
            max_messages=args.max_messages,
        )
    )
    print(f"Processed {processed} messages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
