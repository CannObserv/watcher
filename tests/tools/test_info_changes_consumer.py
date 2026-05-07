"""Reference consumer tests against fakeredis."""

from __future__ import annotations

import asyncio
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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Replace asyncio.sleep with a near-instant no-op so backoff tests stay fast."""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        # Yield control without actually sleeping.
        await real_sleep(0)

    monkeypatch.setattr(consumer.asyncio, "sleep", fast_sleep)


def _valid_envelope_payload() -> bytes:
    """Build a schema_version-2 envelope payload that passes validation."""
    return json.dumps(
        {
            "schema_version": 2,
            "change_id": "01HXY000000000000000000000",
            "watch_id": "01HXY000000000000000000001",
            "info_item_id": "01HXY000000000000000000002",
            "info_spec_id": "01HXY000000000000000000003",
            "previous_snapshot_id": None,
            "current_snapshot_id": "01HXY000000000000000000004",
            "previous_fingerprint": None,
            "current_fingerprint": 12345,
            "detected_at": "2026-05-07T00:00:00.000000Z",
            "significance": 1.0,
            "visual_change_score": None,
            "metadata": {},
        }
    ).encode("utf-8")


async def _pre_create_group(client, topic: str, group: str) -> None:
    """Create group at id='0' so messages added before calling consume() are visible."""
    try:
        await client.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
    except consumer.redis.ResponseError:
        pass


# ---------------------------------------------------------------------------
# Happy path & basic plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_reads_and_writes_jsonl(fake_redis, patch_redis_from_url, tmp_path):
    # Pre-create group at 0 so seeded messages are within its read window.
    await _pre_create_group(fake_redis, "info.changes", "ref-test")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})
    await fake_redis.xadd("info.changes", {"key": "Y", "payload": _valid_envelope_payload()})

    out_file = tmp_path / "info-changes.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="ref-test",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=2,
    )
    assert metrics.messages_consumed == 2
    lines = out_file.read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    # Two valid envelopes serialised in JSONL.
    assert len(records) == 2
    for r in records:
        assert r["payload"]["schema_version"] == 2


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
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})
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
            "payload": _valid_envelope_payload(),
            "hdr.event_type": "fingerprint_shift",
            "hdr.schema_version": "2",
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
    assert record["headers"]["schema_version"] == "2"


# ---------------------------------------------------------------------------
# URL redaction & exception formatting (still pure-function tests)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sub-task 1: Retry/backoff on Redis reconnect
# ---------------------------------------------------------------------------


def test_compute_backoff_sequence_caps_at_30s():
    """Steady exponential 1, 2, 4, 8, 16, 30 (cap), 30, 30."""
    sequence = [consumer._compute_backoff(attempt, initial=1.0, cap=30.0) for attempt in range(8)]
    assert sequence == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_compute_backoff_uses_supplied_initial_and_cap():
    assert consumer._compute_backoff(0, initial=0.5, cap=4.0) == 0.5
    assert consumer._compute_backoff(1, initial=0.5, cap=4.0) == 1.0
    # Hits the cap.
    assert consumer._compute_backoff(10, initial=0.5, cap=4.0) == 4.0


@pytest.mark.asyncio
async def test_consume_recovers_from_transient_connection_error(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """ConnectionError mid-loop is retried; consumer eventually drains the message."""
    await _pre_create_group(fake_redis, "info.changes", "g-recover")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})

    real_from_url = consumer.redis.from_url
    boom_count = {"n": 0}

    def factory(*args, **kwargs):
        client = real_from_url(*args, **kwargs)
        original_xreadgroup = client.xreadgroup

        async def maybe_boom(*a, **kw):
            if boom_count["n"] < 2:
                boom_count["n"] += 1
                raise consumer.redis.ConnectionError("transient")
            return await original_xreadgroup(*a, **kw)

        client.xreadgroup = maybe_boom
        return client

    monkeypatch.setattr(consumer.redis, "from_url", factory)
    out_file = tmp_path / "recovered.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-recover",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
        max_reconnect_attempts=5,
    )
    assert metrics.messages_consumed == 1
    assert boom_count["n"] == 2  # two transient failures, then success


@pytest.mark.asyncio
async def test_consume_exits_after_max_reconnect_attempts(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """If reconnect keeps failing, give up after max_reconnect_attempts."""
    real_from_url = consumer.redis.from_url

    def factory(*args, **kwargs):
        client = real_from_url(*args, **kwargs)

        async def boom(*_a, **_kw):
            raise consumer.redis.ConnectionError("permanent failure")

        client.xreadgroup = boom
        return client

    monkeypatch.setattr(consumer.redis, "from_url", factory)
    out_file = tmp_path / "give_up.jsonl"
    with pytest.raises(SystemExit, match="Redis connection lost"):
        await consumer.consume(
            topic="info.changes",
            group="g-give-up",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            max_messages=1,
            max_reconnect_attempts=3,
        )


@pytest.mark.asyncio
async def test_consume_exits_after_max_reconnect_attempts_during_startup(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """Startup connection failures also retry, then exit after the cap."""

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
            max_reconnect_attempts=2,
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
            max_reconnect_attempts=1,
        )
    msg = str(excinfo.value)
    assert "s3cret" not in msg
    assert "alice" not in msg
    assert "redis.example.com" in msg


# ---------------------------------------------------------------------------
# Sub-task 2: DLQ pattern for poison messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_routes_invalid_json_to_dlq(fake_redis, patch_redis_from_url, tmp_path):
    """A non-JSON payload is republished to info.changes.dead and ACKed on the main stream."""
    await _pre_create_group(fake_redis, "info.changes", "g-poison-json")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": b"not-json"})
    out_file = tmp_path / "poison.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-poison-json",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    assert metrics.messages_consumed == 0
    assert metrics.messages_dlq == 1
    # ACKed on main stream.
    pending = await fake_redis.xpending("info.changes", "g-poison-json")
    assert pending["pending"] == 0
    # Republished to DLQ.
    dead = await fake_redis.xrange("info.changes.dead")
    assert len(dead) == 1
    fields = dead[0][1]
    assert fields[b"failure_reason"].startswith(b"json_decode_error")
    assert fields[b"original_msg_id"]
    assert fields[b"payload"] == b"not-json"


@pytest.mark.asyncio
async def test_consume_routes_schema_version_mismatch_to_dlq(
    fake_redis, patch_redis_from_url, tmp_path
):
    """Envelope with schema_version != 2 → DLQ."""
    await _pre_create_group(fake_redis, "info.changes", "g-poison-schema")
    bad_payload = json.dumps({"schema_version": 1, "change_id": "x"}).encode("utf-8")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": bad_payload})
    out_file = tmp_path / "poison_schema.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-poison-schema",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    assert metrics.messages_consumed == 0
    assert metrics.messages_dlq == 1
    dead = await fake_redis.xrange("info.changes.dead")
    assert len(dead) == 1
    assert dead[0][1][b"failure_reason"].startswith(b"schema_version_mismatch")


@pytest.mark.asyncio
async def test_consume_routes_missing_required_field_to_dlq(
    fake_redis, patch_redis_from_url, tmp_path
):
    """Envelope missing required fields → DLQ."""
    await _pre_create_group(fake_redis, "info.changes", "g-poison-missing")
    bad_payload = json.dumps({"schema_version": 2}).encode("utf-8")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": bad_payload})
    out_file = tmp_path / "poison_missing.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-poison-missing",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    assert metrics.messages_consumed == 0
    assert metrics.messages_dlq == 1
    dead = await fake_redis.xrange("info.changes.dead")
    assert dead[0][1][b"failure_reason"].startswith(b"missing_required_field")


@pytest.mark.asyncio
async def test_consume_routes_missing_payload_field_to_dlq(
    fake_redis, patch_redis_from_url, tmp_path
):
    """Stream entry without 'payload' field at all → DLQ."""
    await _pre_create_group(fake_redis, "info.changes", "g-poison-nopayload")
    await fake_redis.xadd("info.changes", {"key": "X"})
    out_file = tmp_path / "poison_nopayload.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-poison-nopayload",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    assert metrics.messages_dlq == 1
    dead = await fake_redis.xrange("info.changes.dead")
    assert dead[0][1][b"failure_reason"].startswith(b"missing_payload_field")


@pytest.mark.asyncio
async def test_validate_envelope_rejects_non_object_payload():
    """Top-level payload must be a JSON object."""
    parsed, err = consumer._validate_envelope_payload(b'"a string"')
    assert parsed is None
    assert err and err.startswith("payload_not_object")


@pytest.mark.asyncio
async def test_validate_envelope_accepts_valid_payload():
    parsed, err = consumer._validate_envelope_payload(_valid_envelope_payload())
    assert err is None
    assert parsed["schema_version"] == 2


# ---------------------------------------------------------------------------
# Sub-task 3: Structured JSON logging
# ---------------------------------------------------------------------------


def test_consumer_uses_project_logger():
    """Consumer must obtain its logger via src.core.logging.get_logger."""
    import logging as stdlib_logging

    assert isinstance(consumer.logger, stdlib_logging.Logger)
    assert consumer.logger.name == "tools.info_changes_consumer"


def test_consumer_module_does_not_import_print():
    """No top-level print(...) lurking in the module source."""
    import inspect

    source = inspect.getsource(consumer)
    # Allow argparse/help text to reference 'print' in strings; check function calls only.
    # A simple substring scan suffices because the rewritten module shouldn't call print at all.
    assert "print(" not in source, "consumer must not call print(); use the logger"


# ---------------------------------------------------------------------------
# Sub-task 4: Per-message processing timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_timeout_routes_to_dlq(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch
):
    """Slow message handler exceeds timeout → DLQ + ACK on main stream."""
    await _pre_create_group(fake_redis, "info.changes", "g-timeout")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})

    # The autouse _no_real_sleep fixture monkeypatches asyncio.sleep, so we
    # block on an Event that never fires instead — that keeps the coroutine
    # genuinely pending until asyncio.wait_for cancels it.
    never = asyncio.Event()

    async def slow_write(*_a, **_kw):
        await never.wait()

    monkeypatch.setattr(consumer, "_write_record", slow_write)

    out_file = tmp_path / "timeout.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-timeout",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
        process_timeout_s=0.05,
    )
    assert metrics.messages_dlq == 1
    pending = await fake_redis.xpending("info.changes", "g-timeout")
    assert pending["pending"] == 0
    dead = await fake_redis.xrange("info.changes.dead")
    assert dead[0][1][b"failure_reason"].startswith(b"processing_timeout")


def test_default_process_timeout_reads_env_var(monkeypatch):
    """INFO_CHANGES_CONSUMER_TIMEOUT_SECONDS overrides the hard-coded default."""
    monkeypatch.setenv("INFO_CHANGES_CONSUMER_TIMEOUT_SECONDS", "12.5")
    assert consumer._default_process_timeout_s() == 12.5


def test_default_process_timeout_falls_back_to_30(monkeypatch):
    """Unset env var → default 30s."""
    monkeypatch.delenv("INFO_CHANGES_CONSUMER_TIMEOUT_SECONDS", raising=False)
    assert consumer._default_process_timeout_s() == 30.0


def test_default_process_timeout_ignores_garbage(monkeypatch):
    """Non-numeric env var → default 30s (don't crash main())."""
    monkeypatch.setenv("INFO_CHANGES_CONSUMER_TIMEOUT_SECONDS", "not-a-number")
    assert consumer._default_process_timeout_s() == 30.0


# ---------------------------------------------------------------------------
# CLI argument plumbing
# ---------------------------------------------------------------------------


def test_parser_backoff_initial_s_defaults_to_constant():
    """``--backoff-initial-s`` default matches ``DEFAULT_BACKOFF_INITIAL_S``."""
    args = consumer._build_parser().parse_args([])
    assert args.backoff_initial_s == consumer.DEFAULT_BACKOFF_INITIAL_S


def test_parser_backoff_initial_s_accepts_override():
    """``--backoff-initial-s`` is parsed as a float and stored on the namespace."""
    args = consumer._build_parser().parse_args(["--backoff-initial-s", "0.5"])
    assert args.backoff_initial_s == 0.5


@pytest.mark.asyncio
async def test_amain_forwards_backoff_initial_s_to_consume(monkeypatch):
    """``_amain`` plumbs the parsed value through to ``consume`` (regression
    guard for the round-1 fix that exposed the flag on the CLI)."""
    captured: dict = {}

    async def fake_consume(**kwargs):
        captured.update(kwargs)
        return consumer.Metrics()

    monkeypatch.setattr(consumer, "consume", fake_consume)
    args = consumer._build_parser().parse_args(["--backoff-initial-s", "2.5"])
    await consumer._amain(args)
    assert captured["backoff_initial_s"] == 2.5


# ---------------------------------------------------------------------------
# Sub-task 5: Metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_returns_metrics_with_expected_fields(
    fake_redis, patch_redis_from_url, tmp_path
):
    """consume() returns a Metrics with consumed/dlq/last_lag fields."""
    await _pre_create_group(fake_redis, "info.changes", "g-metrics")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})
    out_file = tmp_path / "metrics.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-metrics",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        max_messages=1,
    )
    assert metrics.messages_consumed == 1
    assert metrics.messages_dlq == 0
    assert hasattr(metrics, "last_lag")


@pytest.mark.asyncio
async def test_metrics_emit_writes_json_log(
    fake_redis, patch_redis_from_url, tmp_path, monkeypatch, caplog
):
    """The metrics emitter logs a structured message containing the counters."""
    import logging as stdlib_logging

    caplog.set_level(stdlib_logging.INFO, logger="tools.info_changes_consumer")
    await _pre_create_group(fake_redis, "info.changes", "g-emit")
    metrics = consumer.Metrics()
    metrics.messages_consumed = 7
    metrics.messages_dlq = 2
    metrics.last_lag = 3
    consumer._emit_metrics(metrics)
    # Inspect that a structured payload landed in caplog with the counters.
    found = False
    for rec in caplog.records:
        extra = getattr(rec, "messages_consumed", None)
        if extra == 7:
            assert rec.messages_dlq == 2
            assert rec.last_lag == 3
            found = True
    assert found


# ---------------------------------------------------------------------------
# Sub-task 6: Graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_stops_on_shutdown_event_before_reading(
    fake_redis, patch_redis_from_url, tmp_path
):
    """Setting the shutdown_event before consume starts → returns without claiming any messages."""
    await _pre_create_group(fake_redis, "info.changes", "g-shutdown-pre")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})
    shutdown = asyncio.Event()
    shutdown.set()
    out_file = tmp_path / "shutdown.jsonl"
    metrics = await consumer.consume(
        topic="info.changes",
        group="g-shutdown-pre",
        consumer_name="t1",
        output=out_file,
        block_ms=10,
        shutdown_event=shutdown,
    )
    assert metrics.messages_consumed == 0
    # Message remains undelivered.
    pending = await fake_redis.xpending("info.changes", "g-shutdown-pre")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_consume_drains_in_flight_then_stops_on_shutdown(
    fake_redis, patch_redis_from_url, tmp_path
):
    """Already-claimed messages get ACKed before shutdown returns."""
    await _pre_create_group(fake_redis, "info.changes", "g-shutdown-mid")
    await fake_redis.xadd("info.changes", {"key": "X", "payload": _valid_envelope_payload()})
    await fake_redis.xadd("info.changes", {"key": "Y", "payload": _valid_envelope_payload()})

    shutdown = asyncio.Event()

    # Trigger shutdown after first message is processed.
    real_write = consumer._write_record
    seen = {"n": 0}

    async def write_and_signal(*args, **kwargs):
        await real_write(*args, **kwargs)
        seen["n"] += 1
        if seen["n"] == 1:
            shutdown.set()

    out_file = tmp_path / "drain.jsonl"
    with patch.object(consumer, "_write_record", side_effect=write_and_signal):
        metrics = await consumer.consume(
            topic="info.changes",
            group="g-shutdown-mid",
            consumer_name="t1",
            output=out_file,
            block_ms=10,
            shutdown_event=shutdown,
        )
    # First batch (both msgs) was already claimed; both are drained and ACKed.
    pending = await fake_redis.xpending("info.changes", "g-shutdown-mid")
    assert pending["pending"] == 0
    assert metrics.messages_consumed >= 1
