"""Reference consumer tests against fakeredis."""

import json
from unittest.mock import patch

import fakeredis
import pytest
from fakeredis import aioredis as fakeredis_aio

import tools.info_changes_consumer as consumer


@pytest.fixture
def fake_server():
    """Shared in-memory Redis server; each client connects to the same state."""
    return fakeredis.FakeServer()


@pytest.fixture
async def fake_redis(fake_server):
    """Seed/assertion client — does NOT get closed by consume()."""
    client = fakeredis_aio.FakeRedis(server=fake_server)
    yield client
    await client.aclose()


@pytest.fixture
def patch_redis_from_url(fake_server):
    """Each call to redis.from_url() returns a fresh client on the shared server."""

    def _factory(*_args, **_kwargs):
        return fakeredis_aio.FakeRedis(server=fake_server)

    with patch.object(consumer.redis, "from_url", side_effect=_factory):
        yield


async def _pre_create_group(client, topic: str, group: str) -> None:
    """Create group at id='0' so messages added before calling consume() are visible."""
    try:
        await client.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
    except consumer.redis.ResponseError:
        pass


@pytest.mark.asyncio
async def test_consume_reads_and_writes_jsonl(fake_redis, patch_redis_from_url, tmp_path):
    # Pre-create group at 0 so seeded messages are within its read window.
    await _pre_create_group(fake_redis, "info.changes", "ref-test")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b'{"a": 1}'})
    await fake_redis.xadd("info.changes", {"key": "Y", "payload": b'{"b": 2}'})

    out_file = tmp_path / "info-changes.jsonl"
    processed = await consumer.consume(
        topic="info.changes",
        group="ref-test",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=2,
    )
    assert processed == 2
    lines = out_file.read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    values = {r["payload"]["a"] if "a" in r["payload"] else r["payload"]["b"] for r in records}
    assert values == {1, 2}


@pytest.mark.asyncio
async def test_consume_creates_group_idempotently(fake_redis, patch_redis_from_url, tmp_path):
    out_file = tmp_path / "out.jsonl"
    # First call creates the group (max_messages=0 exits immediately).
    await consumer.consume(
        topic="info.changes",
        group="g1",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=0,
    )
    # Second call must not error on BUSYGROUP.
    await consumer.consume(
        topic="info.changes",
        group="g1",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=0,
    )


@pytest.mark.asyncio
async def test_consume_acks_messages(fake_redis, patch_redis_from_url, tmp_path):
    await _pre_create_group(fake_redis, "info.changes", "g-ack")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b'{"a": 1}'})
    out_file = tmp_path / "out.jsonl"
    await consumer.consume(
        topic="info.changes",
        group="g-ack",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    # Verify pending entries list (PEL) is empty for this consumer.
    pending = await fake_redis.xpending("info.changes", "g-ack")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_consume_extracts_headers(fake_redis, patch_redis_from_url, tmp_path):
    await _pre_create_group(fake_redis, "info.changes", "g-hdr")
    await fake_redis.xadd(
        "info.changes",
        {
            "key": "X",
            "payload": b'{"a": 1}',
            "hdr.event_type": "fingerprint_shift",
            "hdr.schema_version": "1",
        },
    )
    out_file = tmp_path / "headers.jsonl"
    await consumer.consume(
        topic="info.changes",
        group="g-hdr",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    record = json.loads(out_file.read_text().strip())
    assert record["headers"]["event_type"] == "fingerprint_shift"
    assert record["headers"]["schema_version"] == "1"
