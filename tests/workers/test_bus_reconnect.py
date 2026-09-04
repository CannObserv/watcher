"""What the two consumer loops do when the broker connection drops mid-read (#287).

CannObserv/broker#1 R7 asks every participant for a connection policy before the
Phase 3 cutover, and archiver#193's other half noted that a client policy is only
half an answer: the loops still have to survive what the policy now surfaces.
Reading them, they do — both carry the same ``except Exception`` / back off /
continue shape, and neither classifies transient from poison.

What was missing is a test. Every existing loop-failure test in
``test_fetch_facts.py`` and ``test_registry_reconcile.py`` drives a **handler**
failure — a database error raised inside ``process_fact_message`` or
``reconcile_announcement``. A broker error is raised by ``read`` itself, one
frame further out, and nothing exercised that. These pin behaviour that already
holds, which is the point: it becomes load-bearing the moment the broker is a
~40 ms relay away instead of loopback.

``NoPermissionError`` is here because CannObserv/broker#1 D3 puts an ACL in
front of the broker. Its wire shape is a ``ResponseError`` subclass, not a
connection error, so a loop that classified errors by type — as neither of these
does — would be the one to get it wrong.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError
from redis.exceptions import TimeoutError as RedisTimeoutError

import src.workers.fetch_facts as ff_mod
import src.workers.registry_reconcile as rr_mod

# Small enough that the test does not wait on it; the loops take it as an
# argument precisely so this is not a monkeypatch.
_FAST_BACKOFF = 0.01

BROKER_FAILURES = [
    pytest.param(RedisConnectionError("Error 111 connecting to broker:6379."), id="connection"),
    pytest.param(RedisTimeoutError("Timeout reading from broker:6379."), id="timeout"),
    pytest.param(
        NoPermissionError("NOPERM this user has no permissions to run 'xreadgroup'"), id="noperm"
    ),
]


@dataclass
class _FlakyReads:
    """Fails the first N reads, then returns empty until the test stops it.

    ``read`` yields before returning, and that is not cosmetic: in production the
    blocking ``XREAD`` is what paces these loops. A fake that returns without
    awaiting turns the loop into a tight spin that never cedes control, and a
    test driving it from a second task hangs rather than fails.
    """

    failures: int
    error: BaseException
    stop_event: asyncio.Event
    stop_after_reads: int = 4
    reads: int = 0
    acked: list[str] = field(default_factory=list)
    seeks: list[str] = field(default_factory=list)

    async def ensure_group(self, *, start_id: str) -> None:
        await asyncio.sleep(0)

    async def claim_stale(self, *, min_idle_ms: int, count: int) -> list[Any]:
        await asyncio.sleep(0)
        return []

    async def read(self, *, count: int, block_ms: int | None) -> list[Any]:
        await asyncio.sleep(0)
        self.reads += 1
        if self.reads >= self.stop_after_reads:
            self.stop_event.set()
        if self.reads <= self.failures:
            raise self.error
        return []

    async def ack(self, message_id: str) -> None:
        self.acked.append(message_id)

    def seek(self, message_id: str) -> None:
        self.seeks.append(message_id)


class TestBlobsConsumerSurvivesABrokerFailure:
    """``content.blobs`` is the only inbound path for check results. A read error
    that escaped would kill the fact inbox for the rest of the process lifetime,
    with every issued command eventually reaped and ``/health`` still green."""

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failed_read_backs_off_and_keeps_reading(self, error, monkeypatch, caplog):
        stop = asyncio.Event()
        bus = _FlakyReads(failures=2, error=error, stop_event=stop)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)
        monkeypatch.setattr(ff_mod, "migrate_legacy_group", _no_legacy_group)

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            await asyncio.wait_for(
                ff_mod.run_blobs_consumer(
                    MagicMock(),
                    _never_called_session_factory,
                    stop=stop,
                    block_ms=1,
                    error_backoff_seconds=_FAST_BACKOFF,
                ),
                timeout=5,
            )

        # It read past both failures rather than dying on the first.
        assert bus.reads >= 3
        assert any("backing off" in r.getMessage() for r in caplog.records)

    async def test_the_backoff_is_interrupted_by_the_stop_event(self, monkeypatch):
        """Shutdown must not wait out a full backoff. The loop sleeps on
        ``stop.wait()`` rather than ``asyncio.sleep`` for exactly this."""
        stop = asyncio.Event()
        bus = _FlakyReads(
            failures=99,
            error=RedisConnectionError("broker down"),
            stop_event=stop,
            stop_after_reads=1,
        )
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)
        monkeypatch.setattr(ff_mod, "migrate_legacy_group", _no_legacy_group)

        await asyncio.wait_for(
            ff_mod.run_blobs_consumer(
                MagicMock(),
                _never_called_session_factory,
                stop=stop,
                block_ms=1,
                error_backoff_seconds=30.0,  # would hang the test if it were slept through
            ),
            timeout=5,
        )


class TestRegistryConsumerSurvivesABrokerFailure:
    """``info.registry`` is groupless and replays from ``0-0`` every boot, so a
    dead loop does not merely stall — the registry stops converging and the only
    signal is a log line that already scrolled past."""

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failed_read_backs_off_and_keeps_reading(self, error, monkeypatch, caplog):
        stop = asyncio.Event()
        reader = _FlakyReads(failures=2, error=error, stop_event=stop)
        monkeypatch.setattr(rr_mod, "AsyncBusTailReader", lambda *a, **k: reader)

        with caplog.at_level("WARNING", logger="src.workers.registry_reconcile"):
            await asyncio.wait_for(
                rr_mod.run_registry_consumer(
                    MagicMock(),
                    _never_called_session_factory,
                    stop=stop,
                    block_ms=1,
                    error_backoff_seconds=_FAST_BACKOFF,
                ),
                timeout=5,
            )

        assert reader.reads >= 3

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_read_failure_drops_nothing(self, error, monkeypatch, caplog):
        """A broker error arrives with nothing pending, so the drop counter must
        not advance. Reporting it as a dropped announcement would put the one
        message-loss line the loop can emit onto a failure that lost nothing."""
        stop = asyncio.Event()
        reader = _FlakyReads(failures=2, error=error, stop_event=stop)
        monkeypatch.setattr(rr_mod, "AsyncBusTailReader", lambda *a, **k: reader)

        with caplog.at_level("WARNING", logger="src.workers.registry_reconcile"):
            await asyncio.wait_for(
                rr_mod.run_registry_consumer(
                    MagicMock(),
                    _never_called_session_factory,
                    stop=stop,
                    block_ms=1,
                    error_backoff_seconds=_FAST_BACKOFF,
                ),
                timeout=5,
            )

        assert not any("dropping" in r.getMessage() for r in caplog.records)


async def _no_legacy_group(client) -> str:
    return ff_mod.NO_LEGACY_GROUP


def _never_called_session_factory():
    raise AssertionError("no message was read, so no session should have been opened")
