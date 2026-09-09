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
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.streams import dlq_name, group_name
from co_core_aio.bus import AsyncBusConsumer
from redis.exceptions import OutOfMemoryError
from sqlalchemy import select

import src.core.fetch_policy as fetch_policy_core
import src.core.watch_status as watch_status_core
import src.workers.tasks as tasks_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.domain import Domain
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.workers.fetch_commands import publish_pending_fetch_commands, reap_fetch_commands
from src.workers.source_revisions_drain import _TRANSIENT_PUBLISH_ERRORS
from src.workers.tasks import check_watched_item
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

# The broker's own reply text, captured from redis-server 7.0.15.
OOM_MESSAGE = "command not allowed when used memory > 'maxmemory'."


def _oom_client() -> MagicMock:
    """A client whose every ``XADD`` is refused the way the capped broker refuses."""
    client = MagicMock()
    client.xadd = AsyncMock(side_effect=OutOfMemoryError(OOM_MESSAGE))
    return client


def _mock_session_factory(db_session):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


class TestTheErrorIsNotAConnectionError:
    """The trap broker#6 named: OOM arrives as a ``ResponseError``, so a
    producer whose transient set is ``(ConnectionError, TimeoutError)`` treats a
    bounded, retryable refusal as a permanent one."""

    def test_out_of_memory_is_not_a_connection_or_timeout_error(self):
        exc = OutOfMemoryError(OOM_MESSAGE)
        assert not isinstance(exc, (ConnectionError, TimeoutError))

    def test_the_revisions_drain_classifies_it_transient_anyway(self):
        assert OutOfMemoryError in _TRANSIENT_PUBLISH_ERRORS


class TestContentFetchUnderOOM:
    """``content.fetch`` is a **command** stream: a dropped publish is a fetch
    that never happens, and nothing downstream notices the absence. So the only
    acceptable answer is "the row stays, forever if need be"."""

    async def test_the_issue_path_leaves_the_row_pending_publish(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        monkeypatch.setattr(
            tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
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


class TestConfigStateProducersUnderOOM:
    """``content.fetch-policy`` and ``info.watch-status`` are LWW full sets with
    no outbox: the correct response to a refusal is to fail the job loudly and
    let the next periodic tick republish everything. What must NOT happen is a
    swallowed exception, which would report a stale set as delivered."""

    async def test_fetch_policy_publish_raises(self, db_session):
        db_session.add(Domain(name="lcb.wa.gov", min_interval=3.0))
        await db_session.flush()

        with pytest.raises(OutOfMemoryError):
            await fetch_policy_core.publish_full_policy_set(db_session, _oom_client())

    async def test_watch_status_publish_raises(self, db_session):
        await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        with pytest.raises(OutOfMemoryError):
            await watch_status_core.publish_full_status_set(db_session, _oom_client())


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

    async def test_the_dlq_write_is_denyoom_so_quarantine_fails_under_the_cap(self):
        """The one asymmetry worth stating: reading and acking survive the cap,
        but quarantining does not — ``XADD <topic>.dlq`` is refused like every
        other write. A DLQ policy built later must treat the quarantine itself
        as retryable, not as a step that always succeeds."""
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
