"""Tests for the ``info.watch-check`` command consumer (#371).

What Watcher owes Archiver's ``watch_check`` command (cannobserv
``docs/change_bus/watch.md`` at v0.37.0):

* pre-flight every command through check-now's core verdict; a command that
  cannot run becomes a ``check_rejected`` fact through #369's outbox, its
  reason from the closed vocabulary (archived maps to ``paused``);
* an accepted command is a **forced** full fetch that names the command, so
  its completion republishes ``info.watch-status`` (the outcome is a level);
* a redelivered accepted command is recognised, never refused ``fetch_open``;
* the group is ``watcher.watch-check``, created at ``0`` (broker's rule: never
  ``$`` against a stream that may already hold commands), and an undecodable
  command goes to ``info.watch-check.dlq``, which belongs to Watcher.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import get_args

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.dead_letter import split_dead_letter
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import dlq_name, group_name
from co_core.pure.models.changes import (
    CheckRejectedEmit,
    CheckRejectionReason,
    ContentFetchCommand,
    WatchCheckCommandEmit,
    WatchErrorEmit,
)
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from ulid import ULID

import src.workers.fetch_facts as ff_mod
import src.workers.watch_check as wc_mod
from src.core.check_now import CheckRefusal
from src.core.fetch_commands import create_fetch_command
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.models.pending_watch_event import PendingWatchEvent
from src.workers.watch_check import (
    CONSUMER_GROUP,
    CONSUMER_NAME,
    REJECTION_REASONS,
    process_watch_check_message,
    run_watch_check_consumer,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def _command(info_item_id, *, command_id=None, message_id="1-1"):
    emit = WatchCheckCommandEmit(
        occurred_at=NOW,
        command_id=command_id or str(ULID()),
        info_item_id=str(info_item_id),
        requested_at=NOW,
    )
    return from_wire(to_wire(emit), topic=streams.INFO_WATCH_CHECK, message_id=message_id)


async def _rejections(db_session) -> list[PendingWatchEvent]:
    stmt = select(PendingWatchEvent).where(PendingWatchEvent.event_type == "check_rejected")
    return list((await db_session.execute(stmt)).scalars().all())


async def _fetches(db_session, wi) -> list[FetchCommand]:
    stmt = select(FetchCommand).where(FetchCommand.watched_item_id == wi.id)
    return list((await db_session.execute(stmt)).scalars().all())


async def _process(db_session, message, client=None):
    return await process_watch_check_message(
        db_session, message, bus_client=client or fakeredis.FakeAsyncRedis()
    )


class TestAccepted:
    async def test_issues_a_forced_fetch_naming_the_command(self, db_session):
        wi = await make_watched_item(db_session)
        client = fakeredis.FakeAsyncRedis()
        message = _command(wi.archiver_info_item_id)

        outcome = await _process(db_session, message, client)

        assert outcome == "accepted"
        (row,) = await _fetches(db_session, wi)
        assert row.check_command_id == message.payload.command_id
        assert row.forced_full_fetch is True
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert await client.xlen(streams.CONTENT_FETCH) == 1
        assert await _rejections(db_session) == []

    async def test_audits_the_request_with_its_source(self, db_session):
        wi = await make_watched_item(db_session)
        message = _command(wi.archiver_info_item_id)

        await _process(db_session, message)

        stmt = select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CHECK_REQUESTED)
        (event,) = (await db_session.execute(stmt)).scalars().all()
        assert event.payload["source"] == "bus"
        assert event.payload["check_command_id"] == message.payload.command_id

    async def test_a_redelivered_command_is_recognised_not_refused(self, db_session):
        """A crash between the consumer's commit and its ack redelivers the
        command; its fetch is open, and pre-flight alone would answer
        ``fetch_open`` for a check that is running."""
        wi = await make_watched_item(db_session)
        message = _command(wi.archiver_info_item_id)
        await _process(db_session, message)

        outcome = await _process(db_session, message)

        assert outcome == "duplicate"
        assert len(await _fetches(db_session, wi)) == 1
        assert await _rejections(db_session) == []

    async def test_still_recognised_once_its_check_has_closed(self, db_session):
        wi = await make_watched_item(db_session)
        message = _command(wi.archiver_info_item_id)
        await _process(db_session, message)
        (row,) = await _fetches(db_session, wi)
        row.status = FetchCommandStatus.SUCCEEDED
        await db_session.commit()

        assert await _process(db_session, message) == "duplicate"
        assert len(await _fetches(db_session, wi)) == 1


class TestRejected:
    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            # Operator decision on #371: archived has no token; it is inactive.
            ({"archived_at": NOW, "is_active": False}, "paused"),
            ({"is_active": False}, "paused"),
            ({"domain_suspended": True}, "domain_suspended"),
        ],
    )
    async def test_each_refusal_becomes_its_token(self, db_session, fields, reason):
        wi = await make_watched_item(db_session, **fields)
        message = _command(wi.archiver_info_item_id)

        outcome = await _process(db_session, message)

        assert outcome == f"rejected_{reason}"
        (row,) = await _rejections(db_session)
        emit = CheckRejectedEmit.model_validate(row.payload)
        assert (emit.reason, emit.command_id) == (reason, message.payload.command_id)
        assert emit.info_item_id == str(wi.archiver_info_item_id)
        assert emit.info_source_id == wi.archiver_info_source_id
        assert await _fetches(db_session, wi) == []

    async def test_no_url(self, db_session):
        wi = await make_watched_item(db_session)
        wi.effective_url = ""
        await db_session.commit()

        assert await _process(db_session, _command(wi.archiver_info_item_id)) == "rejected_no_url"

    async def test_an_open_fetch(self, db_session):
        wi = await make_watched_item(db_session)
        await create_fetch_command(db_session, wi, now=NOW)
        await db_session.commit()

        outcome = await _process(db_session, _command(wi.archiver_info_item_id))

        assert outcome == "rejected_fetch_open"
        assert len(await _fetches(db_session, wi)) == 1  # nothing new issued

    async def test_a_command_issued_after_pre_flight_still_rejects(self, db_session, monkeypatch):
        """CR 1: pre-flight and the issue read the gate separately. A scheduled
        check that commits between them makes the issue skip; the command must
        still be answered, and the staged audit must not claim a check."""
        wi = await make_watched_item(db_session)
        await db_session.commit()
        real_issue = wc_mod.issue_fetch_command

        async def _scheduled_first(session, item, bus_client, **kwargs):
            await create_fetch_command(session, item, now=NOW)  # the scheduler's
            await session.flush()
            return await real_issue(session, item, bus_client, **kwargs)

        monkeypatch.setattr(wc_mod, "issue_fetch_command", _scheduled_first)

        outcome = await _process(db_session, _command(wi.archiver_info_item_id))

        assert outcome == "rejected_fetch_open"
        (row,) = await _rejections(db_session)
        assert row.payload["reason"] == "fetch_open"
        stmt = select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CHECK_REQUESTED)
        assert (await db_session.execute(stmt)).scalars().all() == []

    async def test_an_unknown_item_names_no_source(self, db_session):
        item_id = ULID()

        outcome = await _process(db_session, _command(item_id))

        assert outcome == "rejected_unknown_item"
        (row,) = await _rejections(db_session)
        emit = CheckRejectedEmit.model_validate(row.payload)
        assert emit.reason == "unknown_item"
        assert emit.info_source_id is None
        assert emit.info_item_id == str(item_id)

    async def test_an_id_that_is_not_a_ulid_is_an_unknown_item(self, db_session):
        """No validator on the command (#283), so any string decodes; Watcher
        holds no such item, and saying so closes the command for Archiver."""
        outcome = await _process(db_session, _command("not-a-ulid"))

        assert outcome == "rejected_unknown_item"
        (row,) = await _rejections(db_session)
        assert row.payload["info_item_id"] == "not-a-ulid"

    async def test_the_rejection_is_committed(self, db_session):
        """The loop acks after ``process`` returns, so the fact must already be
        durable: an uncommitted row is lost with the session."""
        await _process(db_session, _command(ULID()))
        await db_session.rollback()

        assert len(await _rejections(db_session)) == 1


class TestRejectionVocabulary:
    """CR 5: a refusal with no token raises ``KeyError`` inside ``process``,
    and the loop reclaims that command forever, wedging every one behind it."""

    def test_every_refusal_has_a_token(self):
        assert set(REJECTION_REASONS) == set(CheckRefusal)

    def test_every_token_is_in_the_closed_vocabulary(self):
        assert set(REJECTION_REASONS.values()) <= set(get_args(CheckRejectionReason))


class TestForeignPayloads:
    async def test_another_payload_type_is_ignored(self, db_session):
        emit = WatchErrorEmit(
            occurred_at=NOW,
            event_id=str(ULID()),
            info_item_id=str(ULID()),
            info_source_id=str(ULID()),
            reason="http_status",
            status_code=503,
        )
        message = from_wire(to_wire(emit), topic=streams.INFO_WATCH_CHECK, message_id="1-1")

        assert await _process(db_session, message) == "ignored_unknown_type"
        assert await _rejections(db_session) == []


class TestGroup:
    def test_group_is_derived(self):
        assert CONSUMER_GROUP == group_name(streams.INFO_WATCH_CHECK, "watcher")
        assert CONSUMER_GROUP == "watcher.watch-check"

    def test_consumer_name_is_group_derived(self):
        assert CONSUMER_NAME == "watcher-watch-check-1"


@asynccontextmanager
async def _session_ctx(db_session):
    yield db_session


async def _until(predicate, *, timeout=5):
    async def _wait():
        while not await predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait(), timeout=timeout)


class TestRunWatchCheckConsumer:
    async def _run(self, db_session, client, stop):
        return asyncio.create_task(
            run_watch_check_consumer(
                client,
                lambda: _session_ctx(db_session),
                stop=stop,
                block_ms=10,
                error_backoff_seconds=0.01,
            )
        )

    async def test_a_command_issued_before_the_group_existed_is_answered(self, db_session):
        """Broker's rule: the group is created at ``0``. At ``$`` a command
        Archiver issued before Watcher's first boot would never be read."""
        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(_command(ULID()).payload))
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _answered():
            return len(await _rejections(db_session)) == 1

        await _until(_answered)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        pending = await client.xpending(streams.INFO_WATCH_CHECK, CONSUMER_GROUP)
        assert pending["pending"] == 0

    async def test_an_undecodable_command_is_dead_lettered_and_the_loop_moves_on(self, db_session):
        client = fakeredis.FakeAsyncRedis()
        poison_id = await client.xadd(
            streams.INFO_WATCH_CHECK, {"event_type": "watch_check", "payload": "{not json"}
        )
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(_command(ULID()).payload))
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _both_handled():
            return (
                len(await _rejections(db_session)) == 1
                and await client.xlen(dlq_name(streams.INFO_WATCH_CHECK)) == 1
            )

        await _until(_both_handled)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        ((_id, raw),) = await client.xrange(dlq_name(streams.INFO_WATCH_CHECK))
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        original, provenance = split_dead_letter(fields)
        assert original["payload"] == "{not json"
        assert provenance.source_id == poison_id.decode()
        assert provenance.group == CONSUMER_GROUP
        pending = await client.xpending(streams.INFO_WATCH_CHECK, CONSUMER_GROUP)
        assert pending["pending"] == 0

    async def test_an_accepted_command_publishes_its_fetch(self, db_session):
        wi = await make_watched_item(db_session)
        await db_session.commit()
        client = fakeredis.FakeAsyncRedis()
        message = _command(wi.archiver_info_item_id)
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(message.payload))
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _published():
            return await client.xlen(streams.CONTENT_FETCH) == 1

        await _until(_published)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        ((entry_id, raw),) = await client.xrange(streams.CONTENT_FETCH)
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        fetch = from_wire(fields, topic=streams.CONTENT_FETCH, message_id=entry_id.decode())
        assert isinstance(fetch.payload, ContentFetchCommand)
        assert "if-none-match" not in (fetch.payload.headers or {})


@asynccontextmanager
async def _rolling_back_session_ctx(db_session):
    """Production's session factory: a failed ``process`` leaves no open
    transaction behind for the next message to trip over."""
    try:
        yield db_session
    except BaseException:
        await db_session.rollback()
        raise


def _poison_command():
    """#371's example: a NUL in ``info_item_id`` decodes, and is no ULID, so it
    is rejected ``unknown_item`` — and JSONB refuses it at the outbox write."""
    return _command("01POISON\x00COMMANDXXXXXXXXX")


class TestDeliveryThreshold:
    """#389: a decodable command that fails deterministically goes to
    ``info.watch-check.dlq`` after ``MAX_DELIVERY_ATTEMPTS`` and stops blocking
    the commands behind it. Before #389 it was reclaimed forever."""

    async def _run(self, db_session, client, stop):
        return asyncio.create_task(
            run_watch_check_consumer(
                client,
                lambda: _rolling_back_session_ctx(db_session),
                stop=stop,
                block_ms=10,
                error_backoff_seconds=0.01,
            )
        )

    def _count_attempts(self, monkeypatch, *, fail=None):
        """Wrap the real processor; ``fail(message, attempt)`` may raise first.

        Returns ``(attempts, outcomes)`` by ``command_id``. The tests wait on
        these and on Redis, never on ``db_session``: the loop is using it, and
        a concurrent read from the test is an asyncpg ``InterfaceError``.
        """
        attempts: dict[str, int] = {}
        outcomes: dict[str, str] = {}
        real = wc_mod.process_watch_check_message

        async def _counted(session, message, *, bus_client):
            key = message.payload.command_id
            attempts[key] = attempts.get(key, 0) + 1
            if fail is not None:
                fail(message, attempts[key])
            outcomes[key] = await real(session, message, bus_client=bus_client)
            return outcomes[key]

        monkeypatch.setattr(wc_mod, "process_watch_check_message", _counted)
        return attempts, outcomes

    async def test_a_poison_command_is_dead_lettered_and_the_one_behind_it_answered(
        self, db_session, monkeypatch, caplog
    ):
        client = fakeredis.FakeAsyncRedis()
        poison = _poison_command()
        poison_wire = to_wire(poison.payload)
        poison_id = await client.xadd(streams.INFO_WATCH_CHECK, poison_wire)
        behind = _command(ULID())
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(behind.payload))
        attempts, outcomes = self._count_attempts(monkeypatch)
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _both_handled():
            return (
                behind.payload.command_id in outcomes
                and await client.xlen(dlq_name(streams.INFO_WATCH_CHECK)) == 1
            )

        with caplog.at_level("ERROR", logger="src.workers.fetch_facts"):
            await _until(_both_handled)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        # The command_id is what an operator tells Archiver (CR 2).
        (logged,) = [r for r in caplog.records if "dead-lettered" in r.getMessage()]
        assert logged.command_id == poison.payload.command_id
        (rejection,) = await _rejections(db_session)
        assert rejection.payload["command_id"] == behind.payload.command_id
        assert attempts[poison.payload.command_id] == ff_mod.MAX_DELIVERY_ATTEMPTS
        ((_id, raw),) = await client.xrange(dlq_name(streams.INFO_WATCH_CHECK))
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        original, provenance = split_dead_letter(fields)
        assert original == poison_wire
        assert provenance.source_id == poison_id.decode()
        assert provenance.group == CONSUMER_GROUP
        # The class only: an error's text can quote the payload.
        assert provenance.reason == (f"failed {ff_mod.MAX_DELIVERY_ATTEMPTS} attempts: DBAPIError")
        pending = await client.xpending(streams.INFO_WATCH_CHECK, CONSUMER_GROUP)
        assert pending["pending"] == 0

    async def test_an_unreachable_database_never_dead_letters_a_command(
        self, db_session, monkeypatch
    ):
        """A Postgres restart outlasting the threshold must not cost Archiver
        an answer: connection-class errors are not attempts."""
        outage = 3 * ff_mod.MAX_DELIVERY_ATTEMPTS

        def _db_down(message, attempt):
            if attempt <= outage:
                raise OperationalError("SELECT 1", {}, ConnectionRefusedError("db down"))

        attempts, outcomes = self._count_attempts(monkeypatch, fail=_db_down)
        client = fakeredis.FakeAsyncRedis()
        command = _command(ULID())
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(command.payload))
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _answered():
            return command.payload.command_id in outcomes

        await _until(_answered)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert outcomes[command.payload.command_id] == "rejected_unknown_item"
        assert attempts[command.payload.command_id] == outage + 1
        assert await client.xlen(dlq_name(streams.INFO_WATCH_CHECK)) == 0

    async def test_a_refused_dead_letter_is_retried_without_reprocessing(
        self, db_session, monkeypatch
    ):
        """A full broker refuses the DLQ ``XADD`` (``denyoom``). The command
        stays pending and the retirement is retried; the failing ``process`` —
        whose side effects are the problem — is not run again."""
        client = fakeredis.FakeAsyncRedis()
        real_xadd = client.xadd
        refused: list[str] = []

        async def _xadd(name, fields, *args, **kwargs):
            stream = name.decode() if isinstance(name, bytes) else name
            if stream == dlq_name(streams.INFO_WATCH_CHECK) and not refused:
                refused.append(stream)
                raise ResponseError("OOM command not allowed when used memory > 'maxmemory'.")
            return await real_xadd(name, fields, *args, **kwargs)

        monkeypatch.setattr(client, "xadd", _xadd)
        poison = _poison_command()
        await client.xadd(streams.INFO_WATCH_CHECK, to_wire(poison.payload))
        attempts, _outcomes = self._count_attempts(monkeypatch)
        stop = asyncio.Event()

        task = await self._run(db_session, client, stop)

        async def _dead_lettered():
            return await client.xlen(dlq_name(streams.INFO_WATCH_CHECK)) == 1

        await _until(_dead_lettered)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert refused, "the DLQ write was never attempted under refusal"
        assert attempts[poison.payload.command_id] == ff_mod.MAX_DELIVERY_ATTEMPTS
        pending = await client.xpending(streams.INFO_WATCH_CHECK, CONSUMER_GROUP)
        assert pending["pending"] == 0
