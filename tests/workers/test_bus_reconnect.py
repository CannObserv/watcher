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

``OutOfMemoryError`` joins it for #288 (broker#1 R5, tracked by broker#6), the
same shape from a different cause: the broker runs ``noeviction`` under an
explicit ``maxmemory``, so a full instance refuses writes for every client on it.
Measured against a capped scratch broker, of the commands these two loops issue
only ``XGROUP CREATE`` is ``denyoom`` — ``XREADGROUP``, ``XACK``, ``XAUTOCLAIM``
and ``XREAD`` all keep working — so the read parametrisations below are the
hypothetical and ``ensure_group`` is the one that really fires. It is a boot-time
call, which is what makes the re-arm load-bearing: a Watcher restarted while the
broker is full must retry the group until the cap clears, not die before its
first read.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from co_core.pure.adapters.bus.exceptions import BusMessageMissingFieldError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError, OutOfMemoryError
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
    pytest.param(OutOfMemoryError("command not allowed when used memory > 'maxmemory'."), id="oom"),
]


@dataclass
class _FlakyReads:
    """Fails the first N reads, then returns empty until the test stops it.

    ``read`` yields before returning, and that is not cosmetic: in production the
    blocking ``XREAD`` is what paces these loops. A fake that returns without
    awaiting turns the loop into a tight spin that never cedes control, and a
    test driving it from a second task hangs rather than fails.

    ``group_failures`` fails ``ensure_group`` the same way. It is a separate
    counter because that call sits *before* the first read, on a path a read
    failure can never reach.
    """

    failures: int
    error: BaseException
    stop_event: asyncio.Event
    stop_after_reads: int = 4
    group_failures: int = 0
    reads: int = 0
    groups: int = 0
    acked: list[str] = field(default_factory=list)
    seeks: list[str] = field(default_factory=list)

    async def ensure_group(self, *, start_id: str) -> None:
        await asyncio.sleep(0)
        self.groups += 1
        if self.groups <= self.group_failures:
            raise self.error

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

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failed_ensure_group_backs_off_and_still_reaches_the_read(
        self, error, monkeypatch, caplog
    ):
        """A broker outage racing our boot must not kill the task before the loop
        starts. ``ensure_group`` is called *inside* the backoff guard, and the
        ``group_ready`` flag is what keeps it there — hoist either above the
        ``try`` and the fact inbox dies for the process lifetime with ``/health``
        still green.

        Nothing pinned this until now: the fake's ``ensure_group`` never failed,
        and the #285 migration that used to run beside it — the only other thing
        in that pre-read block — went away with #286.
        """
        stop = asyncio.Event()
        bus = _FlakyReads(failures=0, error=error, stop_event=stop, group_failures=2)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

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

        # It retried the group creation rather than dying on the first refusal...
        assert bus.groups == 3
        # ...and got past it to the reads, which is the state that matters.
        assert bus.reads >= 1
        assert any("backing off" in r.getMessage() for r in caplog.records)

    async def test_the_group_is_created_once_and_not_per_pass(self, monkeypatch):
        """``group_ready`` also has a cheaper job: ``ensure_group`` is idempotent
        on the broker but not free, and re-issuing it every poll would put an
        XGROUP CREATE on the hot path of a ~40 ms relay (#287)."""
        stop = asyncio.Event()
        bus = _FlakyReads(failures=0, error=RedisConnectionError("unused"), stop_event=stop)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

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

        assert bus.reads >= 4  # several passes...
        assert bus.groups == 1  # ...one group creation

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


@dataclass
class _AnomalyThenSucceedingAck:
    """Every read yields an undecodable frame, and the ack for it succeeds.

    The anomaly branch's *normal* job, which nothing pinned before #289 wrapped
    its ack in a guard — so a wrapping that swallowed the success case, or broke
    the ``continue``, would have gone unnoticed in the module that is the
    service's only fact inbox.
    """

    stop_event: asyncio.Event
    stop_after_reads: int = 3
    reads: int = 0
    acked: list[str] = field(default_factory=list)

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
        raise BusMessageMissingFieldError(
            "event_type", topic="content.blobs", message_id=f"170000000000-{self.reads}"
        )

    async def ack(self, message_id: str) -> None:
        await asyncio.sleep(0)
        self.acked.append(message_id)

    def seek(self, message_id: str) -> None:
        pass


@dataclass
class _AnomalyThenFailingAck:
    """Every read yields an undecodable frame, and the ack for it fails (#289).

    The two halves are individually ordinary — an undecodable frame is what the
    ``BusMessageAnomaly`` branch exists for, and a broker blip on the ack is the
    class of error the loop's backoff handler was written to absorb. Nothing
    exercised them together, and together they used to kill the task: the ack in
    that branch sits in an ``except`` clause of the ``try`` whose handler would
    have caught it.
    """

    error: BaseException
    stop_event: asyncio.Event
    stop_after_reads: int = 3
    reads: int = 0
    ack_attempts: int = 0

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
        raise BusMessageMissingFieldError(
            "event_type", topic="content.blobs", message_id=f"170000000000-{self.reads}"
        )

    async def ack(self, message_id: str) -> None:
        await asyncio.sleep(0)
        self.ack_attempts += 1
        raise self.error

    def seek(self, message_id: str) -> None:
        pass


class TestBlobsConsumerSurvivesAFailedAnomalyAck:
    """#289: the undecodable-frame ack was the one fallible step outside the
    backoff guard, against an invariant ``run_blobs_consumer``'s own docstring
    states ("Every fallible step is inside the backoff guard").

    Not reachable through OOM — ``XACK`` is not ``denyoom`` (#288) — so this is
    the connection/timeout/NOPERM shape, and NOPERM specifically arrives once
    CannObserv/broker#1 D3 puts an ACL in front of the broker.
    """

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failed_ack_backs_off_and_keeps_reading(self, error, monkeypatch, caplog):
        stop = asyncio.Event()
        bus = _AnomalyThenFailingAck(error=error, stop_event=stop)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

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

        # It kept going: the loop must not return on the first failed ack.
        # Exact, not >=: the fake stops the loop on its third read, so a count
        # above three is the loop spinning — the other half of finding 18.
        assert bus.reads == 3
        assert bus.ack_attempts == 3

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failed_ack_is_reported_as_such(self, error, monkeypatch, caplog):
        """The frame stays unacked and will be re-read, so the operator needs to
        see the ack failing rather than only the undecodable frame — otherwise
        the journal shows the same frame skipped forever with no cause."""
        stop = asyncio.Event()
        bus = _AnomalyThenFailingAck(error=error, stop_event=stop)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

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

        assert any("could not ack" in r.getMessage() for r in caplog.records)

    async def test_the_failed_ack_parks_the_loop_instead_of_spinning(self, monkeypatch):
        """ "Backs off" is the half the sibling test cannot see.

        Without the ``_back_off`` call the loop still "keeps reading" — it just
        does it as fast as the event loop allows, hammering a broker that is
        already failing. Verified by mutation: dropping that one line leaves
        every other test in this class green.

        So park on a backoff long enough that a spinning loop is unmistakable,
        leave ``stop`` unset, and let the event loop run. One attempt means
        parked; a spinning loop racks up hundreds.
        """
        stop = asyncio.Event()
        bus = _AnomalyThenFailingAck(
            error=RedisConnectionError("broker down"),
            stop_event=stop,
            stop_after_reads=10**9,  # never sets stop; this test owns shutdown
        )
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

        task = asyncio.create_task(
            ff_mod.run_blobs_consumer(
                MagicMock(),
                _never_called_session_factory,
                stop=stop,
                block_ms=1,
                error_backoff_seconds=30.0,
            )
        )
        for _ in range(50):  # plenty of turns for a spin to show itself
            await asyncio.sleep(0)

        assert bus.ack_attempts == 1, f"loop is spinning: {bus.ack_attempts} acks in 50 turns"

        stop.set()  # and the park is interruptible, so shutdown is not 30s
        await asyncio.wait_for(task, timeout=5)


class TestBlobsConsumerAcksPastAnUndecodableFrame:
    """The anomaly branch's normal job, unpinned until #289 touched it."""

    async def test_the_frame_is_acked_and_the_loop_carries_on(self, monkeypatch, caplog):
        stop = asyncio.Event()
        bus = _AnomalyThenSucceedingAck(stop_event=stop)
        monkeypatch.setattr(ff_mod, "AsyncBusConsumer", lambda *a, **k: bus)

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

        assert bus.acked == ["170000000000-1", "170000000000-2", "170000000000-3"]
        assert any("undecodable frame" in r.getMessage() for r in caplog.records)
        # The success path must not report an ack failure.
        assert not any("could not ack" in r.getMessage() for r in caplog.records)


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


def _never_called_session_factory():
    raise AssertionError("no message was read, so no session should have been opened")
