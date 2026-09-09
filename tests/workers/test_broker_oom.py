"""What Watcher does when the broker refuses writes with ``OOM …`` (#288).

CannObserv/broker#6 (the OOM contract) and CannObserv/broker#2 (the ACL users)
both needed the same experiment: the broker runs ``maxmemory 512mb`` with
``maxmemory-policy noeviction``, and when the cap is reached ``XADD`` is refused
for **every** producer on the instance, not just the one that filled it. If
Watcher dropped or dead-lettered on that error, the cap would protect the broker
by breaking its clients.

These pin the answer measured against a scratch ``redis-server 7.0.15`` at
``--maxmemory 1mb --maxmemory-policy noeviction`` (see
``docs/BUS-CONNECTION-POLICY.md`` → *Behaviour under ``OOM command not
allowed``*). Two observations from that run are what the tests below encode:

* The exception is ``redis.exceptions.OutOfMemoryError`` — a ``ResponseError``
  subclass, **not** a connection error. A producer classifying by
  ``ConnectionError``/``TimeoutError`` alone would call it permanent, which is
  exactly the failure mode broker#6 asked about.
* Of the commands Watcher issues, only ``XADD`` and ``XGROUP CREATE`` are
  ``denyoom``. ``XREADGROUP``, ``XACK``, ``XAUTOCLAIM``, ``XREAD``, ``XLEN`` and
  ``PING`` all keep working, so the consumers drain their backlog throughout.

The message string is the broker's own, verbatim, so a test reading as a
simulation still names the thing that was observed.

``integration`` is marked **per class**, not on the module: the classification
assertion and the dead-letter command form need no database, and they are the
guards that most want to run in the default pass rather than only under
``-m integration``.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.streams import dlq_name, group_name
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from redis.exceptions import OutOfMemoryError
from sqlalchemy import select

import src.workers.fetch_policy as fetch_policy_mod
import src.workers.tasks as tasks_mod
import src.workers.watch_status as watch_status_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.domain import Domain
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.workers.fetch_commands import publish_pending_fetch_commands, reap_fetch_commands
from src.workers.fetch_policy import publish_fetch_policy
from src.workers.source_revisions_drain import _TRANSIENT_PUBLISH_ERRORS
from src.workers.tasks import check_watched_item
from src.workers.watch_status import publish_watch_status
from tests.conftest import make_watched_item
from tests.workers.bus_helpers import mock_session_factory, wire_task_bus

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

# The broker's own reply text, captured from redis-server 7.0.15.
OOM_MESSAGE = "command not allowed when used memory > 'maxmemory'."


def _oom_client() -> MagicMock:
    """A client whose every ``XADD`` is refused the way the capped broker refuses.

    ``spec=Redis`` so a test that grows a second bus call fails by naming the
    method it did not stub, rather than with ``object MagicMock can't be used in
    'await'`` from a bare mock's auto-attribute.
    """
    client = MagicMock(spec=Redis)
    client.xadd = AsyncMock(side_effect=OutOfMemoryError(OOM_MESSAGE))
    return client


class TestTheErrorIsNotAConnectionError:
    """The trap broker#6 named: OOM arrives as a ``ResponseError``, so a
    producer whose transient set is ``(ConnectionError, TimeoutError)`` treats a
    bounded, retryable refusal as a permanent one."""

    def test_out_of_memory_is_not_a_connection_or_timeout_error(self):
        exc = OutOfMemoryError(OOM_MESSAGE)
        assert not isinstance(exc, (ConnectionError, TimeoutError))

    def test_the_revisions_drain_classifies_it_transient_anyway(self):
        assert OutOfMemoryError in _TRANSIENT_PUBLISH_ERRORS


@pytest.mark.integration
class TestContentFetchUnderOOM:
    """``content.fetch`` is a **command** stream: a dropped publish is a fetch
    that never happens, and nothing downstream notices the absence. So the only
    acceptable answer is "the row stays, forever if need be"."""

    async def test_the_issue_path_leaves_the_row_pending_publish(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: mock_session_factory(db_session)
        )

        result = await check_watched_item(str(wi.id), bus_client=_oom_client())

        assert result["published"] is False
        rows = (await db_session.execute(select(FetchCommand))).scalars().all()
        assert [r.status for r in rows] == [FetchCommandStatus.PENDING_PUBLISH]

    async def test_the_sweep_keeps_the_row_pending_with_no_attempt_ceiling(self, db_session):
        """There is no attempt counter on this outbox, so there is no N at which
        a refused command is abandoned. Ten sweeps stands in for indefinitely."""
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        client = _oom_client()

        for _ in range(10):
            result = await publish_pending_fetch_commands(session=db_session, bus_client=client)
            assert result == {"published": 0}

        assert row.status == FetchCommandStatus.PENDING_PUBLISH
        assert client.xadd.await_count == 10

    async def test_the_reaper_never_expires_a_pending_publish_row(self, db_session):
        """The other way the backlog could vanish. The reaper scans
        ``IN_FLIGHT`` only, so an outage longer than the command timeout does
        not quietly garbage-collect the commands the sweep is still retrying."""
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW - timedelta(days=7))
        await db_session.flush()

        result = await reap_fetch_commands(session=db_session, bus_client=_oom_client())

        assert result == {"reissued": 0, "capped": 0, "reapplied": 0}
        assert row.status == FetchCommandStatus.PENDING_PUBLISH

    async def test_the_reapers_reissue_leaves_the_new_row_pending_publish(self, db_session):
        """The third publish path, and the one that runs during recovery.

        A stalled command is expired and re-issued under a fresh ``command_id``;
        if that publish were treated as terminal the intent would burn a
        ``reissue_count`` per pass and hit ``WATCHER_FETCH_MAX_REISSUES`` while
        the origin was never at fault. It must land ``pending_publish`` for the
        sweep, exactly like the other two paths.
        """
        # One clock: NOW is frozen, so taking published_at off the live clock
        # would build a row published before it was issued once real time moves
        # past NOW — a state production cannot reach.
        issued = datetime.now(UTC) - timedelta(days=7)
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        stalled = await create_fetch_command(db_session, wi, now=issued)
        stalled.status = FetchCommandStatus.IN_FLIGHT
        stalled.published_at = issued
        await db_session.flush()

        result = await reap_fetch_commands(session=db_session, bus_client=_oom_client())

        assert result["reissued"] == 1
        assert result["capped"] == 0
        fresh = (
            (
                await db_session.execute(
                    select(FetchCommand).where(FetchCommand.command_id != stalled.command_id)
                )
            )
            .scalars()
            .one()
        )
        assert fresh.status == FetchCommandStatus.PENDING_PUBLISH
        assert fresh.reissue_count == stalled.reissue_count + 1
        assert fresh.intent_id == stalled.intent_id

    async def test_the_command_publishes_once_the_cap_clears(self, db_session):
        """Recovery is the other half of "retryable": the same row, same
        ``command_id``, goes out on the next sweep after the broker frees memory."""
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        await publish_pending_fetch_commands(session=db_session, bus_client=_oom_client())
        healthy = fakeredis.FakeAsyncRedis()
        result = await publish_pending_fetch_commands(session=db_session, bus_client=healthy)

        assert result == {"published": 1}
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert await healthy.xlen(streams.CONTENT_FETCH) == 1


@pytest.mark.integration
class TestConfigStateProducersUnderOOM:
    """``content.fetch-policy`` and ``info.watch-status`` are LWW full sets with
    no outbox: the correct response to a refusal is to fail the job loudly and
    let the next periodic tick republish everything. What must NOT happen is a
    swallowed exception, which would report a stale set as delivered.

    Driven through the **Procrastinate tasks**, not the core publish helpers.
    The helpers raising is not the property — "the job is recorded failed and
    the cron tick is the retry" is, and a ``try/except`` added to
    ``src/workers/fetch_policy.py`` later would leave a helper-level test green
    while doing exactly the thing the paragraph above forbids."""

    async def test_the_fetch_policy_task_lets_the_refusal_escape(self, db_session, monkeypatch):
        db_session.add(Domain(name="lcb.wa.gov", min_interval=3.0))
        await db_session.flush()
        wire_task_bus(fetch_policy_mod, db_session, monkeypatch, _oom_client)

        with pytest.raises(OutOfMemoryError):
            await publish_fetch_policy()

    async def test_the_watch_status_task_lets_the_refusal_escape(self, db_session, monkeypatch):
        await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wire_task_bus(watch_status_mod, db_session, monkeypatch, _oom_client)

        with pytest.raises(OutOfMemoryError):
            await publish_watch_status()


class TestDeadLetterCommandForm:
    """broker#2 needs the dead-letter write **observed**, not inferred, because
    an ACL that omits a command nobody saw breaks the service at exactly the
    moment it is already failing.

    Captured with ``MONITOR`` against the scratch broker, the seam issues two
    commands and the ACL needs both keys::

        "XADD" "content.blobs.dlq" "*" <fields…>
        "XACK" "content.blobs" "watcher.blobs" "<id>"

    — the ack lands on the **original** stream, not on the ``.dlq`` one.
    """

    async def test_dead_letter_writes_to_the_dlq_and_acks_the_original(self):
        client = fakeredis.FakeAsyncRedis()
        group = group_name(streams.CONTENT_BLOBS, "watcher")
        consumer = AsyncBusConsumer(
            client, topic=streams.CONTENT_BLOBS, group=group, consumer="watcher-blobs-1"
        )
        await consumer.ensure_group(start_id="0")
        message_id = (await client.xadd(streams.CONTENT_BLOBS, {"payload_type": "junk"})).decode()
        await client.xreadgroup(group, "watcher-blobs-1", {streams.CONTENT_BLOBS: ">"}, count=1)

        await consumer.dead_letter(message_id, {"payload_type": "junk"})

        assert dlq_name(streams.CONTENT_BLOBS) == "content.blobs.dlq"
        assert await client.xlen("content.blobs.dlq") == 1
        pending = await client.xpending(streams.CONTENT_BLOBS, group)
        assert pending["pending"] == 0

    async def test_a_refused_dlq_write_propagates_and_leaves_the_frame_unacked(self):
        """That ``XADD <topic>.dlq`` is ``denyoom`` is an observation, not
        something this test establishes — it came off the capped broker. What
        the body checks is the consequence: the refusal propagates and the
        original is **not** acked, so the frame is still there to quarantine
        again. That is the asymmetry a DLQ policy has to plan for — reading and
        acking survive the cap, quarantining does not, so the quarantine is
        itself a retryable step rather than one that always succeeds."""
        client = _oom_client()
        client.xack = AsyncMock(return_value=1)
        consumer = AsyncBusConsumer(
            client,
            topic=streams.CONTENT_BLOBS,
            group=group_name(streams.CONTENT_BLOBS, "watcher"),
            consumer="watcher-blobs-1",
        )

        with pytest.raises(OutOfMemoryError):
            await consumer.dead_letter("1-1", {"payload_type": "junk"})
        client.xack.assert_not_awaited()
