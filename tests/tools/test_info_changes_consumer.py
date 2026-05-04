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
async def test_consume_systemexit_on_startup_connection_error(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """ConnectionError during initial group setup → friendly SystemExit."""

    async def boom(*_args, **_kwargs):
        raise consumer.redis.ConnectionError("connection refused")

    monkeypatch.setattr(consumer, "_ensure_group", boom)
    out_file = tmp_path / "noop.jsonl"
    with pytest.raises(SystemExit, match="Redis unreachable"):
        await consumer.consume(
            topic="info.changes",
            group="g-down",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
        )


@pytest.mark.asyncio
async def test_consume_systemexit_on_startup_timeout(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """TimeoutError during initial group setup → friendly SystemExit."""

    async def boom(*_args, **_kwargs):
        raise consumer.redis.TimeoutError("timeout")

    monkeypatch.setattr(consumer, "_ensure_group", boom)
    out_file = tmp_path / "noop.jsonl"
    with pytest.raises(SystemExit, match="Redis unreachable"):
        await consumer.consume(
            topic="info.changes",
            group="g-down",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
        )


@pytest.mark.asyncio
async def test_consume_systemexit_on_loop_connection_error(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """ConnectionError mid-loop → friendly SystemExit (not raw traceback)."""
    await _pre_create_group(fake_redis, "info.changes", "g-mid")

    real_from_url = consumer.redis.from_url

    def factory(*args, **kwargs):
        client = real_from_url(*args, **kwargs)

        async def boom(*_a, **_kw):
            raise consumer.redis.ConnectionError("connection lost mid-stream")

        client.xreadgroup = boom
        return client

    monkeypatch.setattr(consumer.redis, "from_url", factory)
    out_file = tmp_path / "noop.jsonl"
    with pytest.raises(SystemExit, match="Redis connection lost"):
        await consumer.consume(
            topic="info.changes",
            group="g-mid",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
        )


@pytest.mark.asyncio
async def test_consume_redacts_credentials_in_error(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """Password in REDIS_URL must not appear in SystemExit message."""
    monkeypatch.setattr(
        consumer, "get_redis_url", lambda: "redis://alice:s3cret@redis.example.com:6379/0"
    )

    async def boom(*_args, **_kwargs):
        raise consumer.redis.ConnectionError("nope")

    monkeypatch.setattr(consumer, "_ensure_group", boom)
    out_file = tmp_path / "noop.jsonl"
    with pytest.raises(SystemExit) as excinfo:
        await consumer.consume(
            topic="info.changes",
            group="g-redact",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
        )
    msg = str(excinfo.value)
    assert "s3cret" not in msg
    assert "alice" not in msg
    assert "redis.example.com" in msg


def test_redact_url_strips_userinfo():
    assert (
        consumer._redact_url("redis://alice:s3cret@redis.example.com:6379/0")
        == "redis://redis.example.com:6379/0"
    )


def test_redact_url_no_userinfo_unchanged():
    assert consumer._redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"


def test_redact_url_user_only():
    assert (
        consumer._redact_url("redis://alice@redis.example.com:6379/0")
        == "redis://redis.example.com:6379/0"
    )


def test_redact_url_preserves_ipv6_brackets():
    assert consumer._redact_url("redis://alice:s3cret@[::1]:6379/0") == "redis://[::1]:6379/0"


def test_redact_url_preserves_ipv6_brackets_no_port():
    assert consumer._redact_url("redis://alice:s3cret@[2001:db8::1]/0") == "redis://[2001:db8::1]/0"


def test_safe_exc_message_strips_repr():
    e = consumer.redis.ConnectionError("Error connecting to host:6379")
    assert consumer._safe_exc_message(e) == "ConnectionError: Error connecting to host:6379"


def test_safe_exc_message_handles_no_args():
    e = consumer.redis.ConnectionError()
    assert consumer._safe_exc_message(e) == "ConnectionError: "


@pytest.mark.asyncio
async def test_consume_systemexit_on_xack_connection_error(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """ConnectionError during xack → friendly SystemExit (not raw traceback)."""
    await _pre_create_group(fake_redis, "info.changes", "g-ack-down")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b'{"a": 1}'})

    real_from_url = consumer.redis.from_url

    def factory(*args, **kwargs):
        client = real_from_url(*args, **kwargs)

        async def boom(*_a, **_kw):
            raise consumer.redis.ConnectionError("connection lost during ack")

        client.xack = boom
        return client

    monkeypatch.setattr(consumer.redis, "from_url", factory)
    out_file = tmp_path / "noop.jsonl"
    with pytest.raises(SystemExit, match="Redis connection lost"):
        await consumer.consume(
            topic="info.changes",
            group="g-ack-down",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
        )


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
