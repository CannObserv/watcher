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

#436 adds the other end of a failure run: each loop logs one ``<topic> poll
cycle recovered`` when its first pass after one completes — Replicator's cases
(CannObserv/replicator#188) for both loops, plus the handler-failure and
``ensure_group`` ones, at the bottom of this module.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
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


# --- #436: a failure run ends with one recovery record ------------------------
#
# Replicator's cases (CannObserv/replicator#188, PR #193), for both loops. Each
# step of a script is one read: "fail" raises a broker error, "ok" returns
# nothing (an idle pass), "skip" raises an undecodable frame, "msg" returns one
# message. The loop stops after the script's last read, and that pass still
# finishes, so a recovery on the last step is still logged.

_RECOVERED = "poll cycle recovered"


@dataclass
class _ScriptedReads:
    """One read per script step; ``ensure_group``/``ack`` fail a set number of times."""

    script: list[str]
    stop_event: asyncio.Event
    error: BaseException = field(default_factory=lambda: RedisConnectionError("broker down"))
    group_failures: int = 0
    ack_failures: int = 0
    reads: int = 0
    groups: int = 0
    acks: int = 0
    seeks: list[str] = field(default_factory=list)

    async def ensure_group(self, *, start_id: str) -> None:
        await asyncio.sleep(0)
        self.groups += 1
        if self.groups <= self.group_failures:
            raise self.error

    async def claim_stale(self, *, min_idle_ms: int, count: int) -> list[Any]:
        await asyncio.sleep(0)
        return []

    async def claim_stale_page(self, *, min_idle_ms: int, count: int) -> Any:
        await asyncio.sleep(0)
        return SimpleNamespace(poison=[], messages=[])

    async def read(self, *, count: int, block_ms: int | None) -> list[Any]:
        await asyncio.sleep(0)
        step = self.script[self.reads] if self.reads < len(self.script) else "ok"
        self.reads += 1
        if self.reads >= len(self.script):
            self.stop_event.set()
        if step == "fail":
            raise self.error
        if step == "skip":
            raise BusMessageMissingFieldError(
                "event_type", topic="content.blobs", message_id=f"170000000000-{self.reads}"
            )
        if step == "msg":
            return [
                SimpleNamespace(
                    message_id=f"170000000000-{self.reads}",
                    payload=_Announcement(info_item_id=f"item-{self.reads}"),
                )
            ]
        return []

    async def ack(self, message_id: str) -> None:
        await asyncio.sleep(0)
        self.acks += 1
        if self.acks <= self.ack_failures:
            raise self.error

    def seek(self, message_id: str) -> None:
        self.seeks.append(message_id)


@dataclass
class _Announcement:
    """Stands in for ``RegistryAnnouncementState``; the loop reads only the id."""

    info_item_id: str


@asynccontextmanager
async def _session():
    yield MagicMock()


def _recoveries(caplog, topic: str) -> list[int]:
    """The ``after_failures`` of each recovery record, in order — asserting its shape."""
    found = []
    for r in caplog.records:
        if _RECOVERED in r.getMessage():
            assert r.getMessage() == f"{topic} {_RECOVERED}"
            assert r.levelname == "INFO"
            assert r.topic == topic
            found.append(r.after_failures)
    return found


async def _run_fact_loop(bus, stop, *, process=None, dead_letter_poison=False):
    async def _ok(session, message):
        return "processed"

    await asyncio.wait_for(
        ff_mod.run_fact_consumer(
            bus,
            _session,
            stop=stop,
            topic="content.blobs",
            process=process or _ok,
            block_ms=1,
            error_backoff_seconds=_FAST_BACKOFF,
            dead_letter_poison=dead_letter_poison,
        ),
        timeout=5,
    )


class TestFactConsumerLogsRecovery:
    """``run_fact_consumer`` is the one loop behind all three grouped inboxes, so
    these cover ``content.blobs``, ``content.derived`` and ``info.watch-check``."""

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failure_run_then_a_good_pass_logs_one_recovery(self, error, caplog):
        stop = asyncio.Event()
        bus = _ScriptedReads(["fail", "fail", "ok"], stop, error=error)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert _recoveries(caplog, "content.blobs") == [2]

    async def test_two_failure_runs_log_two_recoveries(self, caplog):
        """A missed reset reads ``[1, 2]``."""
        stop = asyncio.Event()
        bus = _ScriptedReads(["fail", "ok", "fail", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert _recoveries(caplog, "content.blobs") == [1, 1]

    async def test_an_acked_past_frame_ends_the_run(self, caplog):
        """The broker answered (Replicator's CR #9)."""
        stop = asyncio.Event()
        bus = _ScriptedReads(["fail", "skip", "fail", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert _recoveries(caplog, "content.blobs") == [1, 1]

    async def test_a_frame_queued_for_the_dlq_ends_the_run(self, caplog):
        """The ``dead_letter_poison`` branch does not back off; it is not a failure."""
        stop = asyncio.Event()
        bus = _ScriptedReads(["fail", "skip", "fail", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop, dead_letter_poison=True)
        assert _recoveries(caplog, "content.blobs") == [1, 1]

    async def test_no_failure_logs_no_recovery(self, caplog):
        stop = asyncio.Event()
        bus = _ScriptedReads(["ok", "msg", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert _recoveries(caplog, "content.blobs") == []

    async def test_a_failed_undecodable_frame_ack_counts(self, caplog):
        """That ack backs off in a sibling ``except`` (#289), which the generic
        handler cannot see — it needs its own increment."""
        stop = asyncio.Event()
        bus = _ScriptedReads(["skip", "fail", "ok"], stop, ack_failures=1)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert _recoveries(caplog, "content.blobs") == [2]

    async def test_a_handler_failure_counts_and_does_not_end_the_run(self, caplog):
        """Decision 1: every pass that backs off counts, whatever its cause.

        The read succeeding is not the end of the run: a pass whose ``process``
        raises is a failure. A reset placed after the read reads ``[1, 1]``.
        """
        stop = asyncio.Event()
        bus = _ScriptedReads(["msg", "msg", "ok"], stop)
        calls = 0

        async def _fails_twice(session, message):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise ValueError("handler failed")
            return "processed"

        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop, process=_fails_twice)
        assert _recoveries(caplog, "content.blobs") == [2]

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_an_ensure_group_refusal_counts(self, error, caplog):
        stop = asyncio.Event()
        bus = _ScriptedReads(["ok"], stop, error=error, group_failures=2)
        with caplog.at_level("INFO", logger="src.workers.fetch_facts"):
            await _run_fact_loop(bus, stop)
        assert bus.groups == 3
        assert _recoveries(caplog, "content.blobs") == [2]


async def _run_registry_loop(stop):
    await asyncio.wait_for(
        rr_mod.run_registry_consumer(
            MagicMock(),
            _session,
            stop=stop,
            block_ms=1,
            error_backoff_seconds=_FAST_BACKOFF,
        ),
        timeout=5,
    )


class TestRegistryConsumerLogsRecovery:
    """The loop-level counter sits beside ``attempts`` and leaves it alone."""

    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        monkeypatch.setattr(rr_mod, "RegistryAnnouncementState", _Announcement)
        self.reconcile_calls = 0
        self.reconcile_failures = 0

        async def _reconcile(session, announcement):
            self.reconcile_calls += 1
            if self.reconcile_calls <= self.reconcile_failures:
                raise ValueError("reconcile failed")
            return "applied"

        monkeypatch.setattr(rr_mod, "reconcile_announcement", _reconcile)

    def _reader(self, monkeypatch, script, stop, **kw):
        reader = _ScriptedReads(script, stop, **kw)
        monkeypatch.setattr(rr_mod, "AsyncBusTailReader", lambda *a, **k: reader)
        return reader

    @pytest.mark.parametrize("error", BROKER_FAILURES)
    async def test_a_failure_run_then_a_good_pass_logs_one_recovery(
        self, error, monkeypatch, caplog
    ):
        stop = asyncio.Event()
        self._reader(monkeypatch, ["fail", "fail", "ok"], stop, error=error)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert _recoveries(caplog, "info.registry") == [2]

    async def test_two_failure_runs_log_two_recoveries(self, monkeypatch, caplog):
        stop = asyncio.Event()
        self._reader(monkeypatch, ["fail", "ok", "fail", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert _recoveries(caplog, "info.registry") == [1, 1]

    async def test_a_seeked_past_frame_ends_the_run(self, monkeypatch, caplog):
        stop = asyncio.Event()
        reader = self._reader(monkeypatch, ["fail", "skip", "fail", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert reader.seeks == ["170000000000-2"]
        assert _recoveries(caplog, "info.registry") == [1, 1]

    async def test_no_failure_logs_no_recovery(self, monkeypatch, caplog):
        stop = asyncio.Event()
        self._reader(monkeypatch, ["ok", "msg", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert _recoveries(caplog, "info.registry") == []

    async def test_a_reconcile_failure_counts_and_does_not_end_the_run(self, monkeypatch, caplog):
        """A reconcile that raises backs off on a held announcement; the retry
        that applies it ends the run. ``attempts`` keeps its own job."""
        self.reconcile_failures = 2
        stop = asyncio.Event()
        self._reader(monkeypatch, ["msg", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert self.reconcile_calls == 3
        assert not any("dropping" in r.getMessage() for r in caplog.records)
        assert _recoveries(caplog, "info.registry") == [2]

    async def test_a_dropped_announcement_counts_its_backoff(self, monkeypatch, caplog):
        """The give-up pass drops and backs off, so it is a failure too; the run
        ends on the next read."""
        self.reconcile_failures = rr_mod.MAX_APPLY_ATTEMPTS
        stop = asyncio.Event()
        self._reader(monkeypatch, ["msg", "ok"], stop)
        with caplog.at_level("INFO", logger="src.workers.registry_reconcile"):
            await _run_registry_loop(stop)
        assert any("dropping" in r.getMessage() for r in caplog.records)
        assert _recoveries(caplog, "info.registry") == [rr_mod.MAX_APPLY_ATTEMPTS]
