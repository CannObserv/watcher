"""Reference consumer for `info.changes` — XREADGROUP loop with JSONL output.

Usage:
    uv run python tools/info_changes_consumer.py \\
        --group archive-ref --output /tmp/info-changes.jsonl

Run alongside Watcher to verify the wire end-to-end. Acks each message after
writing it to the output file. On startup, creates the consumer group if it
doesn't exist (MKSTREAM ensures the stream exists).

Operational features (issue #143):

* **Retry/backoff on Redis reconnect.** Connection drops at startup or
  mid-loop trigger steady exponential backoff (1, 2, 4, 8, 16, 30 s; capped
  at 30 s). The 30 s ceiling matches Redis Sentinel's typical failover
  budget — long enough to absorb a leader election, short enough that the
  consumer doesn't accumulate noticeable lag once Redis is back. Exits with
  a friendly SystemExit only after ``max_reconnect_attempts`` (default:
  unlimited) is exhausted.

* **Dead-letter queue.** Messages that fail validation (non-JSON payload,
  missing required envelope fields, schema_version mismatch) or exceed the
  per-message processing timeout are republished to ``<topic>.dead`` with a
  ``failure_reason`` field, then ``XACK``'d on the main stream so the
  consumer group does not redeliver them. The DLQ entry preserves the
  original ``key``, ``payload``, ``hdr.*`` fields, plus
  ``original_msg_id`` and ``original_topic``.

* **Per-message processing timeout.** Each message is processed under
  ``asyncio.wait_for`` (default 30 s). On timeout the message is routed to
  the DLQ and ACK'd so a slow consumer cannot starve the group.

* **Structured JSON logging.** The entry point calls
  ``configure_logging()``; all log lines flow through the project's
  ``JsonFormatter``. State is passed via ``extra=`` rather than f-strings.

* **Metrics.** Cumulative counters (``messages_consumed``,
  ``messages_dlq``, ``last_lag``) are emitted as a JSON log line every
  ``metrics_interval_s`` (default 60 s). ``last_lag`` is ``XLEN`` minus the
  total messages consumed in this process and is best-effort — it is reset
  to ``-1`` if Redis is unreachable when the emitter runs.

* **Graceful shutdown.** SIGTERM and SIGINT set an internal
  ``asyncio.Event``; the loop stops claiming new batches, drains any
  in-flight batch (writing + ACKing each message), then returns the final
  metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as redis

from src.core.changes.redis_url import get_redis_url
from src.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

DEFAULT_TOPIC = "info.changes"
DEFAULT_DLQ_SUFFIX = ".dead"
DEFAULT_GROUP = "reference-consumer"

# 30 s matches Sentinel-style failover budgets — see module docstring.
DEFAULT_BACKOFF_INITIAL_S = 1.0
DEFAULT_BACKOFF_CAP_S = 30.0

DEFAULT_PROCESS_TIMEOUT_S = 30.0
DEFAULT_METRICS_INTERVAL_S = 60.0

# Env var override for the per-message processing timeout. Read at CLI
# argument-parse time so tests / operators can flex it without a redeploy.
PROCESS_TIMEOUT_ENV_VAR = "INFO_CHANGES_CONSUMER_TIMEOUT_SECONDS"


def _default_process_timeout_s() -> float:
    """Resolve the default process timeout from env, with a stable 30 s fallback.

    Non-numeric values are ignored (logged-on-debug at most) so a malformed
    env var can't crash ``main()`` at startup.
    """
    raw = os.environ.get(PROCESS_TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_PROCESS_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_PROCESS_TIMEOUT_S


# Required keys in a schema_version-2 envelope (see workers/changes_drain.py).
_REQUIRED_ENVELOPE_FIELDS = (
    "schema_version",
    "change_id",
    "watch_id",
    "info_item_id",
    "info_spec_id",
    "current_snapshot_id",
    "detected_at",
)
_SUPPORTED_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    """Cumulative consumer counters; emitted to logs every minute."""

    messages_consumed: int = 0
    messages_dlq: int = 0
    last_lag: int = -1
    # Stream id of the most recently ACKed message; informational.
    last_consumed_msg_id: str = ""


def _emit_metrics(metrics: Metrics) -> None:
    """Log a structured JSON line carrying current metric values."""
    logger.info(
        "consumer metrics",
        extra={
            "messages_consumed": metrics.messages_consumed,
            "messages_dlq": metrics.messages_dlq,
            "last_lag": metrics.last_lag,
            "last_consumed_msg_id": metrics.last_consumed_msg_id,
        },
    )


async def _refresh_lag(client: redis.Redis, topic: str, metrics: Metrics) -> None:
    """Update ``metrics.last_lag`` to ``XLEN(topic) - messages_consumed``.

    Best-effort — failures (Redis unreachable, missing stream) silently set
    ``last_lag`` to -1 so the metrics emitter can keep ticking.
    """
    try:
        length = await client.xlen(topic)
    except (redis.ConnectionError, redis.TimeoutError, redis.ResponseError):
        metrics.last_lag = -1
        return
    metrics.last_lag = max(0, int(length) - metrics.messages_consumed)


async def _metrics_loop(
    client: redis.Redis,
    topic: str,
    metrics: Metrics,
    interval_s: float,
    shutdown_event: asyncio.Event,
) -> None:
    """Background task: emit metrics every ``interval_s`` until shutdown."""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_s)
        except TimeoutError:
            pass
        await _refresh_lag(client, topic, metrics)
        _emit_metrics(metrics)


# ---------------------------------------------------------------------------
# URL / exception helpers (preserved from the original module)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def _compute_backoff(
    attempt: int,
    *,
    initial: float = DEFAULT_BACKOFF_INITIAL_S,
    cap: float = DEFAULT_BACKOFF_CAP_S,
) -> float:
    """Steady exponential backoff (no jitter), capped at ``cap`` seconds.

    ``attempt`` is zero-indexed, so attempt 0 returns ``initial``, attempt 1
    returns ``initial * 2``, and so on.
    """
    return min(cap, initial * (2**attempt))


async def _backoff_sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch reconnect-loop sleeps without
    touching ``asyncio.sleep`` globally (which other code paths still need)."""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Envelope validation
# ---------------------------------------------------------------------------


def _validate_envelope_payload(payload: bytes) -> tuple[dict | None, str | None]:
    """Parse + validate a schema_version-2 envelope.

    Returns ``(parsed_dict, None)`` on success or ``(None, failure_reason)``
    on validation failure. ``failure_reason`` is a stable, machine-readable
    string suitable for the DLQ ``failure_reason`` field.
    """
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, f"json_decode_error: {type(e).__name__}"
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as e:
        return None, f"json_decode_error: {e.msg}"
    if not isinstance(parsed, dict):
        return None, f"payload_not_object: got {type(parsed).__name__}"
    if parsed.get("schema_version") != _SUPPORTED_SCHEMA_VERSION:
        got = parsed.get("schema_version")
        return None, f"schema_version_mismatch: expected {_SUPPORTED_SCHEMA_VERSION}, got {got!r}"
    missing = [f for f in _REQUIRED_ENVELOPE_FIELDS if f not in parsed]
    if missing:
        return None, f"missing_required_field: {','.join(missing)}"
    return parsed, None


# ---------------------------------------------------------------------------
# Output formatting + write
# ---------------------------------------------------------------------------


def _format(msg_id: bytes, fields: dict[bytes, bytes], parsed_payload: dict) -> dict:
    """Render a stream entry as a JSONL-friendly dict."""
    out: dict = {
        "_msg_id": msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id),
        "payload": parsed_payload,
    }
    if b"key" in fields:
        out["key"] = fields[b"key"].decode("utf-8")
    headers: dict[str, str] = {}
    for k, v in fields.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        if key.startswith("hdr."):
            headers[key[4:]] = v.decode("utf-8") if isinstance(v, bytes) else str(v)
    if headers:
        out["headers"] = headers
    return out


async def _write_record(fp, record: dict) -> None:
    """Persist a single record to the output file (flush after each)."""
    fp.write(json.dumps(record) + "\n")
    fp.flush()


# ---------------------------------------------------------------------------
# DLQ
# ---------------------------------------------------------------------------


async def _publish_to_dlq(
    client: redis.Redis,
    *,
    dlq_topic: str,
    original_topic: str,
    msg_id: bytes,
    fields: dict[bytes, bytes],
    failure_reason: str,
) -> None:
    """Republish a poison message to the DLQ stream with a failure reason.

    Preserves the original ``key``, ``payload``, and ``hdr.*`` fields so the
    operator can replay or inspect them. Adds ``failure_reason``,
    ``original_msg_id``, ``original_topic``.
    """
    out_fields: dict[str | bytes, str | bytes] = {}
    for k, v in fields.items():
        out_fields[k] = v
    out_fields["failure_reason"] = failure_reason.encode("utf-8")
    out_fields["original_msg_id"] = (
        msg_id if isinstance(msg_id, bytes) else str(msg_id).encode("utf-8")
    )
    out_fields["original_topic"] = original_topic.encode("utf-8")
    await client.xadd(dlq_topic, out_fields)


# ---------------------------------------------------------------------------
# Group / connection helpers
# ---------------------------------------------------------------------------


async def _ensure_group(client: redis.Redis, topic: str, group: str) -> None:
    """Create the consumer group if it doesn't exist; ignore BUSYGROUP."""
    try:
        await client.xgroup_create(name=topic, groupname=group, id="$", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


async def _process_one_message(
    *,
    client: redis.Redis,
    topic: str,
    group: str,
    dlq_topic: str,
    msg_id: bytes,
    fields: dict[bytes, bytes],
    fp,
    process_timeout_s: float,
    metrics: Metrics,
) -> None:
    """Validate, write, and ACK one message — or DLQ it on any failure.

    Either path ends in an ``XACK`` so the consumer group does not redeliver
    poison or timed-out messages.
    """
    msg_id_str = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)

    # Stream entry must carry a payload field at all.
    if b"payload" not in fields:
        await _publish_to_dlq(
            client,
            dlq_topic=dlq_topic,
            original_topic=topic,
            msg_id=msg_id,
            fields=fields,
            failure_reason="missing_payload_field",
        )
        await client.xack(topic, group, msg_id)
        metrics.messages_dlq += 1
        logger.warning(
            "message routed to DLQ",
            extra={"msg_id": msg_id_str, "failure_reason": "missing_payload_field"},
        )
        return

    payload = fields[b"payload"]
    parsed, failure_reason = _validate_envelope_payload(payload)
    if failure_reason is not None:
        await _publish_to_dlq(
            client,
            dlq_topic=dlq_topic,
            original_topic=topic,
            msg_id=msg_id,
            fields=fields,
            failure_reason=failure_reason,
        )
        await client.xack(topic, group, msg_id)
        metrics.messages_dlq += 1
        logger.warning(
            "message routed to DLQ",
            extra={"msg_id": msg_id_str, "failure_reason": failure_reason},
        )
        return

    record = _format(msg_id, fields, parsed)
    try:
        await asyncio.wait_for(_write_record(fp, record), timeout=process_timeout_s)
    except TimeoutError:
        await _publish_to_dlq(
            client,
            dlq_topic=dlq_topic,
            original_topic=topic,
            msg_id=msg_id,
            fields=fields,
            failure_reason=f"processing_timeout: {process_timeout_s}s",
        )
        await client.xack(topic, group, msg_id)
        metrics.messages_dlq += 1
        logger.warning(
            "message routed to DLQ (processing timeout)",
            extra={"msg_id": msg_id_str, "timeout_s": process_timeout_s},
        )
        return

    await client.xack(topic, group, msg_id)
    metrics.messages_consumed += 1
    metrics.last_consumed_msg_id = msg_id_str


# ---------------------------------------------------------------------------
# Consume loop
# ---------------------------------------------------------------------------


@dataclass
class _ConsumeContext:
    """Per-call configuration bundle for the consume loop."""

    topic: str
    group: str
    consumer_name: str
    dlq_topic: str
    block_ms: int
    max_messages: int | None
    process_timeout_s: float
    max_reconnect_attempts: int | None
    backoff_initial_s: float
    backoff_cap_s: float
    metrics_interval_s: float
    redis_url: str = ""
    safe_url: str = ""
    metrics: Metrics = field(default_factory=Metrics)


async def _connect(url: str) -> redis.Redis:
    """Build a fresh Redis client. Wrapped to make patching trivial in tests."""
    return redis.from_url(url)


async def _consume_loop(
    ctx: _ConsumeContext,
    output: Path,
    shutdown_event: asyncio.Event,
) -> None:
    """Outer connect/retry loop wrapping the inner XREADGROUP loop.

    Connection failures (startup or steady-state) trigger exponential
    backoff and reconnect. After ``max_reconnect_attempts`` the loop raises
    SystemExit with a friendly, credential-redacted message.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    fp = output.open("a", encoding="utf-8")
    try:
        while not shutdown_event.is_set():
            client = await _connect(ctx.redis_url)
            metrics_task: asyncio.Task | None = None
            try:
                try:
                    await _ensure_group(client, ctx.topic, ctx.group)
                except (redis.ConnectionError, redis.TimeoutError) as e:
                    logger.warning(
                        "Redis unreachable during group setup",
                        extra={
                            "redis_url": ctx.safe_url,
                            "error": _safe_exc_message(e),
                            "attempt": attempt,
                        },
                    )
                    if (
                        ctx.max_reconnect_attempts is not None
                        and attempt + 1 >= ctx.max_reconnect_attempts
                    ):
                        raise SystemExit(
                            f"Redis unreachable at {ctx.safe_url}: {_safe_exc_message(e)}. "
                            "Is redis-server running? "
                            "(sudo systemctl status redis-server)"
                        ) from e
                    delay = _compute_backoff(
                        attempt, initial=ctx.backoff_initial_s, cap=ctx.backoff_cap_s
                    )
                    attempt += 1
                    await _backoff_sleep(delay)
                    continue

                # Group established — start metrics emitter. Backoff is reset
                # by the read loop after the first successful XREADGROUP, not
                # here, so a Redis that accepts XGROUP CREATE but immediately
                # drops the read connection still progresses toward the
                # max_reconnect_attempts ceiling.
                metrics_task = asyncio.create_task(
                    _metrics_loop(
                        client,
                        ctx.topic,
                        ctx.metrics,
                        ctx.metrics_interval_s,
                        shutdown_event,
                    )
                )
                pre_read_consumed = ctx.metrics.messages_consumed
                pre_read_dlq = ctx.metrics.messages_dlq
                await _read_loop(client, ctx, fp, shutdown_event)
                if (
                    ctx.metrics.messages_consumed > pre_read_consumed
                    or ctx.metrics.messages_dlq > pre_read_dlq
                ):
                    attempt = 0
                # Either max_messages reached or shutdown was signalled — exit cleanly.
                return
            except (redis.ConnectionError, redis.TimeoutError) as e:
                logger.warning(
                    "Redis connection lost during consume",
                    extra={
                        "redis_url": ctx.safe_url,
                        "error": _safe_exc_message(e),
                        "attempt": attempt,
                    },
                )
                if (
                    ctx.max_reconnect_attempts is not None
                    and attempt + 1 >= ctx.max_reconnect_attempts
                ):
                    raise SystemExit(
                        f"Redis connection lost at {ctx.safe_url}: {_safe_exc_message(e)}. "
                        "Is redis-server running? (sudo systemctl status redis-server)"
                    ) from e
                delay = _compute_backoff(
                    attempt, initial=ctx.backoff_initial_s, cap=ctx.backoff_cap_s
                )
                attempt += 1
                await asyncio.sleep(delay)
            finally:
                if metrics_task is not None:
                    metrics_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await metrics_task
                await client.aclose()
    finally:
        fp.close()


async def _read_loop(
    client: redis.Redis,
    ctx: _ConsumeContext,
    fp,
    shutdown_event: asyncio.Event,
) -> None:
    """Inner XREADGROUP loop. Returns when max_messages or shutdown reached.

    Connection errors propagate to the outer reconnect loop. In-flight
    batches are always drained — even if shutdown is signalled mid-batch,
    each already-claimed message is processed and ACKed before returning.
    """
    while True:
        if (
            ctx.max_messages is not None
            and (ctx.metrics.messages_consumed + ctx.metrics.messages_dlq) >= ctx.max_messages
        ):
            return
        if shutdown_event.is_set():
            return

        response = await client.xreadgroup(
            groupname=ctx.group,
            consumername=ctx.consumer_name,
            streams={ctx.topic: ">"},
            count=10,
            block=ctx.block_ms,
        )
        if not response:
            continue

        for _stream, entries in response:
            for msg_id, fields in entries:
                await _process_one_message(
                    client=client,
                    topic=ctx.topic,
                    group=ctx.group,
                    dlq_topic=ctx.dlq_topic,
                    msg_id=msg_id,
                    fields=fields,
                    fp=fp,
                    process_timeout_s=ctx.process_timeout_s,
                    metrics=ctx.metrics,
                )
                if (
                    ctx.max_messages is not None
                    and ctx.metrics.messages_consumed >= ctx.max_messages
                ):
                    return
            # Finished draining this batch; honour shutdown before claiming more.
            if shutdown_event.is_set():
                return


async def consume(
    *,
    topic: str,
    group: str,
    consumer_name: str,
    output: Path,
    block_ms: int = 5000,
    max_messages: int | None = None,
    dlq_topic: str | None = None,
    process_timeout_s: float = DEFAULT_PROCESS_TIMEOUT_S,
    max_reconnect_attempts: int | None = None,
    backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
    metrics_interval_s: float = DEFAULT_METRICS_INTERVAL_S,
    shutdown_event: asyncio.Event | None = None,
) -> Metrics:
    """Run the consume loop. Returns a ``Metrics`` snapshot on exit.

    See module docstring for the full operational contract.
    """
    redis_url = get_redis_url()
    ctx = _ConsumeContext(
        topic=topic,
        group=group,
        consumer_name=consumer_name,
        dlq_topic=dlq_topic or f"{topic}{DEFAULT_DLQ_SUFFIX}",
        block_ms=block_ms,
        max_messages=max_messages,
        process_timeout_s=process_timeout_s,
        max_reconnect_attempts=max_reconnect_attempts,
        backoff_initial_s=backoff_initial_s,
        backoff_cap_s=backoff_cap_s,
        metrics_interval_s=metrics_interval_s,
        redis_url=redis_url,
        safe_url=_redact_url(redis_url),
    )
    if shutdown_event is None:
        shutdown_event = asyncio.Event()
    await _consume_loop(ctx, output, shutdown_event)
    return ctx.metrics


# ---------------------------------------------------------------------------
# Signal-aware entry point
# ---------------------------------------------------------------------------


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    """Wire SIGTERM and SIGINT to set the shutdown event.

    Uses ``loop.add_signal_handler`` where available (POSIX); on platforms
    where it is not supported we silently fall back to default signal
    behaviour — the consumer is POSIX-only in production.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, event.set)
        except (NotImplementedError, RuntimeError):
            pass


async def _amain(args: argparse.Namespace) -> Metrics:
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, shutdown)
    return await consume(
        topic=args.topic,
        group=args.group,
        consumer_name=args.consumer,
        output=args.output,
        max_messages=args.max_messages,
        dlq_topic=args.dlq_topic,
        process_timeout_s=args.process_timeout_s,
        max_reconnect_attempts=args.max_reconnect_attempts,
        backoff_cap_s=args.backoff_cap_s,
        metrics_interval_s=args.metrics_interval_s,
        shutdown_event=shutdown,
    )


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Reference consumer for info.changes")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--consumer", default="ref-1")
    parser.add_argument("--output", type=Path, default=Path("/tmp/info-changes.jsonl"))
    parser.add_argument("--dlq-topic", default=None, help="defaults to <topic>.dead")
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Exit after this many messages (default: run forever)",
    )
    parser.add_argument(
        "--process-timeout-s",
        type=float,
        default=_default_process_timeout_s(),
        help=(
            "Per-message processing timeout in seconds (DLQ on overrun). "
            f"Defaults to ${PROCESS_TIMEOUT_ENV_VAR} or 30 s."
        ),
    )
    parser.add_argument(
        "--max-reconnect-attempts",
        type=int,
        default=None,
        help="Give up after this many consecutive reconnects (default: unlimited)",
    )
    parser.add_argument(
        "--backoff-cap-s",
        type=float,
        default=DEFAULT_BACKOFF_CAP_S,
        help="Maximum backoff between reconnect attempts",
    )
    parser.add_argument(
        "--metrics-interval-s",
        type=float,
        default=DEFAULT_METRICS_INTERVAL_S,
        help="Cadence for emitting cumulative metrics",
    )
    args = parser.parse_args()

    configure_logging()
    metrics = asyncio.run(_amain(args))
    logger.info(
        "consumer exiting",
        extra={
            "messages_consumed": metrics.messages_consumed,
            "messages_dlq": metrics.messages_dlq,
            "last_lag": metrics.last_lag,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
