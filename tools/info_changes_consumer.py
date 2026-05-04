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

import redis.asyncio as redis

from src.core.changes.redis_url import get_redis_url

DEFAULT_TOPIC = "info.changes"
DEFAULT_GROUP = "reference-consumer"


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
    client = redis.from_url(get_redis_url())
    await _ensure_group(client, topic, group)
    processed = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("a", encoding="utf-8") as fp:
            while True:
                if max_messages is not None and processed >= max_messages:
                    break
                response = await client.xreadgroup(
                    groupname=group,
                    consumername=consumer_name,
                    streams={topic: ">"},
                    count=10,
                    block=block_ms,
                )
                if not response:
                    continue
                for _stream, entries in response:
                    for msg_id, fields in entries:
                        record = _format(msg_id, fields)
                        fp.write(json.dumps(record) + "\n")
                        fp.flush()
                        await client.xack(topic, group, msg_id)
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
