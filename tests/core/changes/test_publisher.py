"""ChangePublisher tests using fakeredis (no real Redis required)."""

import pytest
from fakeredis import aioredis as fakeredis_aio

from src.core.changes.publisher import ChangePublisher


@pytest.fixture
async def fake_redis():
    client = fakeredis_aio.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def publisher(fake_redis):
    p = ChangePublisher(redis_client=fake_redis)
    yield p


@pytest.mark.asyncio
async def test_publish_writes_to_stream(publisher, fake_redis):
    msg_id = await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b'{"ok": true}',
        headers={"event_type": "fingerprint_shift"},
    )
    assert msg_id is not None
    entries = await fake_redis.xrange("info.changes")
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_publish_partition_key_recorded(publisher, fake_redis):
    await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b'{"hello": "world"}',
        headers={},
    )
    entries = await fake_redis.xrange("info.changes")
    fields = entries[0][1]
    assert fields[b"key"] == b"01HZZ00000000000000000000A"
    assert fields[b"payload"] == b'{"hello": "world"}'


@pytest.mark.asyncio
async def test_publish_returns_message_id_format(publisher):
    msg_id = await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b"x",
        headers={},
    )
    # Redis Streams IDs are <ms>-<seq>
    assert "-" in msg_id


@pytest.mark.asyncio
async def test_publish_includes_headers_as_separate_fields(publisher, fake_redis):
    await publisher.publish_change(
        topic="info.changes",
        key="01HZZ00000000000000000000A",
        payload=b"x",
        headers={"event_type": "spec.healed_via_fallback", "schema_version": "1"},
    )
    entries = await fake_redis.xrange("info.changes")
    fields = entries[0][1]
    assert fields[b"hdr.event_type"] == b"spec.healed_via_fallback"
    assert fields[b"hdr.schema_version"] == b"1"


@pytest.mark.asyncio
async def test_topic_isolation(publisher, fake_redis):
    await publisher.publish_change(topic="info.changes", key="x", payload=b"a", headers={})
    await publisher.publish_change(topic="info.spec_changes", key="y", payload=b"b", headers={})

    changes = await fake_redis.xrange("info.changes")
    spec_changes = await fake_redis.xrange("info.spec_changes")
    assert len(changes) == 1
    assert len(spec_changes) == 1
