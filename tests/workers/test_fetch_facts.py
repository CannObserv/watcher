"""Tests for the content.blobs fact consumer (#241, Phase 4 step 2).

Contract MUSTs pinned here:

* MUST-3 — correlation on ``command_id`` only; a fact for an unknown id is
  discarded (acked), never matched by URL.
* MUST-4 — at-least-once per command: a duplicate fact re-runs the upsert
  harmlessly; a late duplicate after the apply settled changes nothing.
* MUST-5 — no fingerprint dedupe: two commands returning identical bytes (same
  fingerprint, same blob_uri, different command_ids) both correlate.

Plus the #252 posture on ``info_source_id`` (cannobserv#300): it is reporting,
not routing — see ``TestOrphanFacts``.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg.exceptions as pg_errors
import fakeredis
import psycopg
import pytest
from co_core.effects.bus import BusPublish
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import dlq_name, group_name
from co_core.pure.models.changes import (
    BlobAvailableEmit,
    FetchFailedEmit,
    RegistryAnnouncementEmit,
)
from co_core_aio.bus import AsyncBusPublisher
from procrastinate.exceptions import ConnectorException
from redis import exceptions as redis_errors
from sqlalchemy import exc as sa_exc

import src.workers.fetch_facts as ff_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommandStatus
from src.workers.fetch_facts import (
    CONSUMER_GROUP,
    CONSUMER_NAME,
    is_connection_error,
    process_fact_message,
    run_blobs_consumer,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 6, 17, 0, 0, tzinfo=UTC)


# The domain key on a fact that answers no command of ours (cannobserv#300 makes
# it required, so an orphan is now attributable — see TestOrphanFacts).
ORPHAN_INFO_SOURCE_ID = "01ORPHANINFOSOURCEIDXXXXXX"


def _blob_message(
    command_id,
    *,
    url="https://lcb.wa.gov/notices",
    fingerprint="ab" * 32,
    info_source_id=ORPHAN_INFO_SOURCE_ID,
    **over,
):
    event = BlobAvailableEmit(
        occurred_at=over.pop("occurred_at", NOW),
        content_fingerprint=fingerprint,
        blob_uri=over.pop("blob_uri", f"file:///var/lib/replicator/blobs/{fingerprint}.bin"),
        size_bytes=over.pop("size_bytes", 1234),
        media_type=over.pop("media_type", "text/html"),
        url=url,
        command_id=command_id,
        info_source_id=info_source_id,
        **over,
    )
    return from_wire(to_wire(event), topic=streams.CONTENT_BLOBS, message_id="1-1")


def _failure_message(
    command_id,
    *,
    terminal=True,
    reason="http_status",
    status_code=404,
    info_source_id=ORPHAN_INFO_SOURCE_ID,
):
    event = FetchFailedEmit(
        occurred_at=NOW,
        command_id=command_id,
        url="https://lcb.wa.gov/notices",
        info_source_id=info_source_id,
        reason=reason,
        terminal=terminal,
        status_code=status_code,
        detail="origin said no",
    )
    return from_wire(to_wire(event), topic=streams.CONTENT_BLOBS, message_id="1-2")


def _blob_for(row, **over):
    """A blob fact answering ``row`` — both ids as a real Replicator echo carries
    them (cannobserv#300: ``info_source_id`` is echoed verbatim from the command)."""
    return _blob_message(row.command_id, info_source_id=row.info_source_id, **over)


def _failure_for(row, **over):
    """A failure fact answering ``row`` — see ``_blob_for``."""
    return _failure_message(row.command_id, info_source_id=row.info_source_id, **over)


class _DeferSpy:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, command_id: str) -> None:
        self.calls.append(command_id)


async def _issued_row(db_session, **wi_kwargs):
    wi = await make_watched_item(
        db_session, primary_url=wi_kwargs.pop("primary_url", "https://lcb.wa.gov/notices")
    )
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = FetchCommandStatus.IN_FLIGHT
    row.published_at = NOW
    await db_session.flush()
    return wi, row


class TestBlobFacts:
    async def test_blob_fact_upserts_row_and_defers_apply(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail = _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _blob_for(
                row,
                content_type_raw="text/html; charset=utf-8",
                final_url="https://lcb.wa.gov/notices/",
                status_code=200,
                fetched_at=NOW,
            ),
            defer_blob=blob,
            defer_failure=fail,
        )

        assert outcome == "blob_recorded"
        assert row.content_fingerprint == "ab" * 32
        assert row.blob_uri.startswith("file://")
        assert row.size_bytes == 1234
        assert row.content_type_raw == "text/html; charset=utf-8"
        assert row.final_url == "https://lcb.wa.gov/notices/"
        assert row.status_code == 200
        assert row.fact_at == NOW
        assert blob.calls == [row.command_id]
        assert fail.calls == []

    async def test_blob_expires_at_is_persisted(self, db_session):
        """#253: the blob's horizon is Replicator's to state and ours to carry.

        ``SourceRevisionObservedEvent.blob_expires_at`` is echoed from this fact,
        never derived locally — the issuer contract's MUST-7 TTL is Replicator's
        policy on a clock that runs from last fetch reference, which no consumer
        observes. Dropping it here would leave the observed event with no honest
        value to send, so Archiver would record absence for every revision.
        """
        _, row = await _issued_row(db_session)
        horizon = NOW + timedelta(days=7)

        outcome = await process_fact_message(
            db_session,
            _blob_for(row, blob_expires_at=horizon),
            defer_blob=_DeferSpy(),
        )

        assert outcome == "blob_recorded"
        assert row.blob_expires_at == horizon

    async def test_absent_blob_expires_at_stays_null(self, db_session):
        """``None`` means the horizon is unknown — record absence, never a guess."""
        _, row = await _issued_row(db_session)

        await process_fact_message(db_session, _blob_for(row), defer_blob=_DeferSpy())

        assert row.blob_expires_at is None

    async def test_duplicate_fact_is_idempotent(self, db_session):
        _, row = await _issued_row(db_session)
        blob = _DeferSpy()
        message = _blob_for(row)

        first = await process_fact_message(db_session, message, defer_blob=blob)
        second = await process_fact_message(db_session, message, defer_blob=blob)

        assert (first, second) == ("blob_recorded", "blob_recorded")
        assert blob.calls == [row.command_id, row.command_id]  # apply task guards

    async def test_late_duplicate_after_apply_changes_nothing(self, db_session):
        _, row = await _issued_row(db_session)
        row.status = FetchCommandStatus.SUCCEEDED
        row.applied_at = NOW
        await db_session.flush()
        blob = _DeferSpy()

        outcome = await process_fact_message(db_session, _blob_for(row), defer_blob=blob)

        assert outcome == "already_applied"
        assert blob.calls == []
        assert row.status == FetchCommandStatus.SUCCEEDED

    async def test_unknown_command_id_is_discarded(self, db_session):
        blob = _DeferSpy()
        outcome = await process_fact_message(
            db_session, _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX"), defer_blob=blob
        )
        assert outcome == "unmatched"
        assert blob.calls == []

    async def test_same_fingerprint_two_commands_both_correlate(self, db_session):
        # MUST-5: fingerprint is content identity, command_id is correlation
        # identity — identical bytes must not suppress the second fact.
        _, row_a = await _issued_row(db_session, primary_url="https://a.example/x")
        _, row_b = await _issued_row(db_session, primary_url="https://b.example/x")
        blob = _DeferSpy()

        for row in (row_a, row_b):
            outcome = await process_fact_message(
                db_session, _blob_for(row, fingerprint="cd" * 32), defer_blob=blob
            )
            assert outcome == "blob_recorded"

        assert row_a.content_fingerprint == row_b.content_fingerprint == "cd" * 32
        assert blob.calls == [row_a.command_id, row_b.command_id]


class TestFailureFacts:
    async def test_terminal_failure_marks_row_and_defers(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail = _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row),
            defer_blob=blob,
            defer_failure=fail,
        )

        assert outcome == "failure_recorded"
        assert row.status == FetchCommandStatus.FAILED
        assert row.failure_reason == "http_status"
        assert row.failure_detail == "origin said no"
        assert row.status_code == 404
        assert fail.calls == [row.command_id]
        assert blob.calls == []

    async def test_nonterminal_failure_refreshes_fact_at_only(self, db_session):
        _, row = await _issued_row(db_session)
        fail = _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row, terminal=False, reason="http_status"),
            defer_failure=fail,
        )

        assert outcome == "nonterminal_recorded"
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert row.fact_at == NOW
        assert fail.calls == []

    async def test_unknown_failure_is_discarded(self, db_session):
        fail = _DeferSpy()
        outcome = await process_fact_message(
            db_session, _failure_message("01UNKNOWNCOMMANDIDXXXXXXXX"), defer_failure=fail
        )
        assert outcome == "unmatched"
        assert fail.calls == []


class TestUnrecognisedFailureReasons:
    """``reason`` is a stable-but-additive token, so an unknown one is opaque.

    co-core types it ``str`` rather than ``Literal`` precisely so a producer
    adding a token never crashes an older consumer, and the contract tells
    consumers to "branch on ``terminal`` first and treat an unknown ``reason``
    as opaque". Nothing in this repo enumerates the tokens, so the promise is
    only as good as a test that a *novel* one lands on the generic path.

    Live occasion: CannObserv/replicator#95 adds a destination-guard refusal
    (watcher#304). The token below is deliberately synthetic rather than that
    one — pinning the property, not a single string, is what keeps the next
    addition free too.
    """

    async def test_a_novel_terminal_token_records_verbatim_and_defers_the_failure(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail, unchanged = _DeferSpy(), _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            # A token no version of co-core has ever emitted, and a shape the
            # guard-style refusals share: refused before any request, so no
            # status code (cf. ``invalid_request_options``).
            _failure_for(row, reason="token_from_a_newer_producer", status_code=None),
            defer_blob=blob,
            defer_failure=fail,
            defer_not_modified=unchanged,
        )

        assert outcome == "failure_recorded"
        assert row.status == FetchCommandStatus.FAILED
        assert row.failure_reason == "token_from_a_newer_producer"
        assert row.status_code is None
        # The generic failure apply, not the one success-shaped exception.
        assert fail.calls == [row.command_id]
        assert unchanged.calls == []
        assert blob.calls == []

    async def test_a_novel_non_terminal_token_still_branches_on_terminal_first(self, db_session):
        # ``terminal`` is the field that decides, not the token: an unrecognised
        # reason on a command that is still retrying must not close the row.
        _, row = await _issued_row(db_session)
        fail = _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row, terminal=False, reason="token_from_a_newer_producer"),
            defer_failure=fail,
        )

        assert outcome == "nonterminal_recorded"
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert row.failure_reason is None
        assert fail.calls == []


class TestNotModifiedFacts:
    """#249 part 1: ``not_modified`` rides ``FetchFailedEvent`` but is a success.

    co-core's own registry says so (``changes.py`` — "the one token on this event
    that is **not** a failure"). The consumer's job here is to close the row
    without routing it to the failure apply, which would mark a healthy item
    ERROR and notify a user about it on every no-change check.
    """

    async def test_not_modified_closes_the_row_as_its_own_status(self, db_session):
        _, row = await _issued_row(db_session)
        blob, fail, unchanged = _DeferSpy(), _DeferSpy(), _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row, reason="not_modified", status_code=304),
            defer_blob=blob,
            defer_failure=fail,
            defer_not_modified=unchanged,
        )

        assert outcome == "not_modified_recorded"
        assert row.status == FetchCommandStatus.NOT_MODIFIED
        assert row.status_code == 304
        assert row.fact_at == NOW
        assert unchanged.calls == [row.command_id]
        assert fail.calls == []
        assert blob.calls == []

    async def test_not_modified_is_never_journalled_as_a_failure(self, db_session):
        # co-core note 2: at steady state this token outnumbers every real
        # failure combined, so anything counting failure_reason must not see it.
        _, row = await _issued_row(db_session)

        await process_fact_message(
            db_session,
            _failure_for(row, reason="not_modified", status_code=304),
            defer_not_modified=_DeferSpy(),
        )

        assert row.failure_reason is None
        assert row.failure_detail is None

    async def test_not_modified_records_no_fingerprint(self, db_session):
        # There are no bytes for this command, so ``content_fingerprint`` (the
        # RAW-bytes identity Replicator published for *this* command) stays NULL
        # rather than inheriting the previous occasion's value.
        _, row = await _issued_row(db_session)

        await process_fact_message(
            db_session,
            _failure_for(row, reason="not_modified", status_code=304),
            defer_not_modified=_DeferSpy(),
        )

        assert row.content_fingerprint is None
        assert row.blob_uri is None

    async def test_not_modified_after_apply_changes_nothing(self, db_session):
        _, row = await _issued_row(db_session)
        row.applied_at = NOW
        row.status = FetchCommandStatus.SUCCEEDED
        await db_session.flush()
        unchanged = _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row, reason="not_modified", status_code=304),
            defer_not_modified=unchanged,
        )

        assert outcome == "already_applied"
        assert row.status == FetchCommandStatus.SUCCEEDED
        assert unchanged.calls == []

    async def test_nonterminal_not_modified_still_only_refreshes_fact_at(self, db_session):
        # ``terminal`` is branched first (contract), and the token does not
        # promote a non-terminal fact into a closed row.
        _, row = await _issued_row(db_session)
        unchanged = _DeferSpy()

        outcome = await process_fact_message(
            db_session,
            _failure_for(row, terminal=False, reason="not_modified", status_code=304),
            defer_not_modified=unchanged,
        )

        assert outcome == "nonterminal_recorded"
        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert unchanged.calls == []

    async def test_unknown_not_modified_is_discarded(self, db_session):
        unchanged = _DeferSpy()
        outcome = await process_fact_message(
            db_session,
            _failure_message("01UNKNOWNCOMMANDIDXXXXXXXX", reason="not_modified", status_code=304),
            defer_not_modified=unchanged,
        )
        assert outcome == "unmatched"
        assert unchanged.calls == []


class TestOrphanFacts:
    """#252: ``info_source_id`` makes an unmatched fact *attributable*, not
    recoverable.

    ``content.blobs`` is broadcast, so a fact naming one of our InfoSources may
    answer another issuer's command entirely — and applying bytes fetched under
    a different User-Agent would manufacture a spurious change signal, the exact
    failure ``WATCHER_USER_AGENT`` is pinned to prevent. The discard stands;
    what the field buys is a log line an operator can act on.
    """

    async def test_attributable_blob_orphan_is_still_discarded(self, db_session, caplog):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        await db_session.flush()
        blob = _DeferSpy()

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            outcome = await process_fact_message(
                db_session,
                _blob_message(
                    "01UNKNOWNCOMMANDIDXXXXXXXX",
                    info_source_id=wi.archiver_info_source_id,
                ),
                defer_blob=blob,
            )

        assert outcome == "unmatched"
        assert blob.calls == []
        (record,) = [r for r in caplog.records if "matched no fetch command" in r.message]
        assert record.info_source_id == wi.archiver_info_source_id
        assert record.watched_item_id == str(wi.id)

    async def test_attributable_failure_orphan_is_still_discarded(self, db_session, caplog):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        await db_session.flush()
        fail = _DeferSpy()

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            outcome = await process_fact_message(
                db_session,
                _failure_message(
                    "01UNKNOWNCOMMANDIDXXXXXXXX",
                    info_source_id=wi.archiver_info_source_id,
                ),
                defer_failure=fail,
            )

        assert outcome == "unmatched"
        assert fail.calls == []
        (record,) = [r for r in caplog.records if "matched no fetch command" in r.message]
        assert record.watched_item_id == str(wi.id)

    async def test_unattributable_orphan_names_no_watched_item(self, db_session, caplog):
        blob = _DeferSpy()

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            outcome = await process_fact_message(
                db_session, _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX"), defer_blob=blob
            )

        assert outcome == "unmatched"
        (record,) = [r for r in caplog.records if "matched no fetch command" in r.message]
        assert record.watched_item_id is None

    async def test_two_items_on_one_info_source_do_not_poison_the_message(self, db_session, caplog):
        # Nothing constrains one WatchedItem per InfoSource. If the attribution
        # lookup raised on a second row, the message would go unacked and be
        # re-read forever — a log line must not do that.
        shared = "01SHAREDINFOSOURCEIDXXXXXX"
        for url in ("https://a.example/x", "https://b.example/x"):
            await make_watched_item(db_session, primary_url=url, archiver_info_source_id=shared)
        await db_session.flush()
        blob = _DeferSpy()

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            outcome = await process_fact_message(
                db_session,
                _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX", info_source_id=shared),
                defer_blob=blob,
            )

        assert outcome == "unmatched"
        assert blob.calls == []

    async def test_echo_mismatch_warns_but_still_correlates(self, db_session, caplog):
        # MUST-3: command_id is the correlator. A mismatched echo is an
        # integrity signal, never grounds to refuse a fact.
        _, row = await _issued_row(db_session)
        blob = _DeferSpy()

        with caplog.at_level("WARNING", logger="src.workers.fetch_facts"):
            outcome = await process_fact_message(
                db_session,
                _blob_message(row.command_id, info_source_id="01SOMEONEELSESSOURCEXXXXXX"),
                defer_blob=blob,
            )

        assert outcome == "blob_recorded"
        assert blob.calls == [row.command_id]
        assert any("info_source_id echo mismatch" in r.message for r in caplog.records)


class TestRunBlobsConsumer:
    """CR-1: the loop must survive processing errors — an escaped exception
    kills the fact inbox for the rest of the process lifetime."""

    async def test_loop_survives_a_processing_error_and_retries(self, db_session, monkeypatch):
        client = fakeredis.FakeAsyncRedis()
        # Group must exist BEFORE the fact lands ('$' start), mirroring prod.
        stop = asyncio.Event()

        calls: list[int] = []
        real_process = ff_mod.process_fact_message

        async def _flaky(session, message, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient DB error")
            return await real_process(session, message, **kwargs)

        monkeypatch.setattr(ff_mod, "process_fact_message", _flaky)

        @asynccontextmanager
        async def _ctx():
            yield db_session

        task = asyncio.create_task(
            run_blobs_consumer(
                client,
                lambda: _ctx(),
                stop=stop,
                block_ms=10,
                error_backoff_seconds=0.01,
            )
        )
        # Let ensure_group run, then publish one fact.
        await asyncio.sleep(0.1)
        event = _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX").payload
        await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_BLOBS, to_wire(event)))

        async def _until_processed():
            while len(calls) < 2:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_until_processed(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)  # exits cleanly, no exception

        assert len(calls) == 2  # failed once, retried via reclaim, succeeded
        pending = await client.xpending(streams.CONTENT_BLOBS, CONSUMER_GROUP)
        assert pending["pending"] == 0  # retried message was acked

    async def test_a_fact_that_always_fails_is_acked_past_and_stops_blocking(
        self, db_session, monkeypatch, caplog
    ):
        """#389: past ``MAX_DELIVERY_ATTEMPTS`` a fact inbox acks the fact past,
        its undecodable-frame policy — no DLQ of ours (``content.blobs.dlq`` is
        ungranted), and the reaper re-issues the fetch whose fact never lands.
        Before #389 the fact was reclaimed forever and every fact behind it
        waited."""
        client = fakeredis.FakeAsyncRedis()
        stop = asyncio.Event()
        poison_id, behind_id = "01POISONFACTCOMMANDXXXXXXX", "01BEHINDFACTCOMMANDXXXXXXX"
        attempts: dict[str, int] = {}
        outcomes: dict[str, str] = {}
        real_process = ff_mod.process_fact_message

        async def _poisoned(session, message, **kwargs):
            key = message.payload.command_id
            attempts[key] = attempts.get(key, 0) + 1
            if key == poison_id:
                raise ValueError("deterministic")
            outcomes[key] = await real_process(session, message, **kwargs)
            return outcomes[key]

        monkeypatch.setattr(ff_mod, "process_fact_message", _poisoned)

        @asynccontextmanager
        async def _ctx():
            yield db_session

        task = asyncio.create_task(
            run_blobs_consumer(
                client, lambda: _ctx(), stop=stop, block_ms=10, error_backoff_seconds=0.01
            )
        )
        await asyncio.sleep(0.1)  # the group starts at '$'
        publisher = AsyncBusPublisher(client)
        for command_id in (poison_id, behind_id):
            event = _blob_message(command_id).payload
            await publisher.execute(BusPublish(streams.CONTENT_BLOBS, to_wire(event)))

        async def _behind_processed():
            while behind_id not in outcomes:
                await asyncio.sleep(0.02)

        with caplog.at_level("ERROR", logger="src.workers.fetch_facts"):
            await asyncio.wait_for(_behind_processed(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        # No DLQ on a fact inbox: the journal is the only record of the drop,
        # so it names the command whose fetch the reaper must re-issue (CR 2).
        (retired,) = [r for r in caplog.records if "acked past" in r.getMessage()]
        assert retired.command_id == poison_id
        assert retired.reason == f"failed {ff_mod.MAX_DELIVERY_ATTEMPTS} attempts: ValueError"
        assert attempts[poison_id] == ff_mod.MAX_DELIVERY_ATTEMPTS
        assert outcomes[behind_id] == "unmatched"
        pending = await client.xpending(streams.CONTENT_BLOBS, CONSUMER_GROUP)
        assert pending["pending"] == 0
        assert await client.exists(dlq_name(streams.CONTENT_BLOBS)) == 0


def _sa_error(cls=sa_exc.DBAPIError, *, invalidated=False):
    return cls("SELECT 1", {}, Exception("orig"), connection_invalidated=invalidated)


class TestConnectionErrorsAreNotAttempts:
    """#389: what the delivery threshold refuses to count. An unreachable DB or
    broker fails every message alike, so counting it would retire whatever is at
    the head of the stream during an outage. Anything else counts — an
    unrecognised transient costs one retired message, not a stuck inbox."""

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(_sa_error(sa_exc.OperationalError), id="sa-operational"),
            pytest.param(_sa_error(invalidated=True), id="sa-connection-invalidated"),
            pytest.param(redis_errors.ConnectionError("down"), id="redis-connection"),
            pytest.param(redis_errors.TimeoutError("slow"), id="redis-timeout"),
            pytest.param(ConnectionRefusedError("refused"), id="os-refused"),
            pytest.param(TimeoutError(), id="builtin-timeout"),
            # Postgres refusing service, raised raw by asyncpg's connect —
            # SQLAlchemy neither translates nor flags it (CR 6).
            pytest.param(
                pg_errors.CannotConnectNowError("the database system is starting up"),
                id="pg-57P03-starting-up",
            ),
            pytest.param(pg_errors.AdminShutdownError("terminating"), id="pg-57P01-admin"),
            pytest.param(pg_errors.CrashShutdownError("crash"), id="pg-57P02-crash"),
            pytest.param(pg_errors.TooManyConnectionsError("slots"), id="pg-53300-slots"),
            pytest.param(pg_errors.ConnectionFailureError("lost"), id="pg-08006-failure"),
        ],
    )
    def test_unreachable_dependencies_do_not_count(self, exc):
        assert is_connection_error(exc)

    def test_procrastinates_wrapped_psycopg_outage_does_not_count(self):
        """The fact inboxes commit, then defer the apply over Procrastinate's own
        psycopg pool, which re-raises a psycopg error as ``ConnectorException``.
        Counted, a pool outage after the commit acks the fact past with the row
        recorded and no apply deferred — and nothing re-defers it (CR 1)."""
        try:
            try:
                raise psycopg.OperationalError("connection failed: Connection refused")
            except psycopg.OperationalError as inner:
                raise ConnectorException() from inner
        except ConnectorException as exc:
            assert is_connection_error(exc)

    def test_a_wrapped_connection_error_does_not_count(self):
        try:
            try:
                raise ConnectionResetError("peer reset")
            except ConnectionResetError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as exc:
            assert is_connection_error(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            # #371's NUL in JSONB arrives as asyncpg's generic Error.
            pytest.param(_sa_error(), id="sa-dbapi-data"),
            pytest.param(_sa_error(sa_exc.IntegrityError), id="sa-integrity"),
            # asyncpg raises InterfaceError for session-state misuse ("another
            # operation is in progress"), which recurs on every attempt.
            pytest.param(_sa_error(sa_exc.InterfaceError), id="sa-interface"),
            pytest.param(redis_errors.ResponseError("NOPERM"), id="redis-response"),
            # A statement timeout can be the message's own fault: it counts.
            pytest.param(pg_errors.QueryCanceledError("timeout"), id="pg-57014-canceled"),
            pytest.param(pg_errors.UniqueViolationError("dup"), id="pg-23505-unique"),
            # OSError, but the filesystem's: as deterministic as a bad payload (CR 7).
            pytest.param(FileNotFoundError("/var/lib/x"), id="os-file-not-found"),
            pytest.param(PermissionError("/var/lib/x"), id="os-permission"),
            pytest.param(IsADirectoryError("/var/lib"), id="os-is-a-directory"),
            pytest.param(ValueError("bad"), id="value-error"),
        ],
    )
    def test_everything_else_counts(self, exc):
        assert not is_connection_error(exc)


class TestForeignPayloadTypes:
    """The co-core 0.9.3 union widening must not reach this decode path (#254).

    ``payload_from_dict`` now dispatches ``registry_announcement`` and
    ``watch_status`` too. Neither can legitimately arrive here — they ride
    ``info.registry`` / ``info.watch-status``, and this consumer reads
    ``content.blobs`` — but the fact inbox must degrade to a log line rather
    than a raise if one ever does, because an escaped exception parks the
    message unacked and the loop re-reads it forever.
    """

    async def test_registry_announcement_on_the_fact_stream_is_ignored(self, db_session, caplog):
        event = RegistryAnnouncementEmit(
            occurred_at=NOW,
            info_item_id="01INFOITEMXXXXXXXXXXXXXXXX",
            generation=1,
            info_source_id="01INFOSOURCEXXXXXXXXXXXXXX",
            url="https://lcb.wa.gov/notices",
            source_specs=[{"selector": "main"}],
            watch_spec={"schema_version": 1, "interval": "1d"},
            active=True,
        )
        message = from_wire(to_wire(event), topic=streams.CONTENT_BLOBS, message_id="9-9")

        outcome = await process_fact_message(db_session, message)

        assert outcome == "ignored_unknown_type"


class TestValidatorFacts:
    """#269 part 2: the conditional-GET validators land on the row.

    They arrive on every blob fact and were dropped until now. The row is the
    *provenance* half — what this occasion returned; the item-level pair the next
    command replays is written by the apply path, under its ordering guard
    (MUST-5: a validator pinned to a fingerprint replays a stale
    ``If-None-Match`` for exactly as long as the content is unchanged).
    """

    async def test_blob_fact_records_both_validators(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.commit()

        await process_fact_message(
            db_session,
            _blob_for(row, etag='W/"v2"', last_modified="Wed, 13 Aug 2026 10:00:00 GMT"),
            defer_blob=_DeferSpy(),
        )

        await db_session.refresh(row)
        assert row.etag == 'W/"v2"'
        assert row.last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"

    async def test_absent_validators_stay_null(self, db_session):
        # None means "nobody said" — never a default to substitute for.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.commit()

        await process_fact_message(db_session, _blob_for(row), defer_blob=_DeferSpy())

        await db_session.refresh(row)
        assert row.etag is None
        assert row.last_modified is None

    async def test_a_not_modified_fact_records_no_validators(self, db_session):
        # A 304 carries none, and the stored pair is current by definition.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.commit()

        await process_fact_message(
            db_session,
            _failure_for(row, reason="not_modified", status_code=304),
            defer_not_modified=_DeferSpy(),
        )

        await db_session.refresh(row)
        assert row.etag is None
        assert row.last_modified is None


class TestGroupNaming:
    """#285 — the group is *derived* from the stream, never hand-written.

    cannobserv#384 landed ``<service>.<stream-suffix>[-<purpose>]`` and, with it,
    ``group_name``. The 0/5 conformance rate that issue documents was the direct
    product of a convention that existed only as prose beside a free-string
    ``group`` parameter, so the helper — not the literal — is the contract.
    """

    def test_group_is_derived_from_the_stream(self):
        assert CONSUMER_GROUP == group_name(streams.CONTENT_BLOBS, "watcher")
        assert CONSUMER_GROUP == "watcher.blobs"

    def test_no_purpose_segment_for_a_lone_group(self):
        """Watcher runs exactly one group on content.blobs, so the segment that
        disambiguates two groups of the same service must be absent."""
        assert CONSUMER_GROUP.count(".") == 1
        assert "-" not in CONSUMER_GROUP

    def test_consumer_name_stays_group_derived_and_host_independent(self):
        """The audit's conclusion on the *name* was 'verified, nothing to do' —
        it changes only as a consequence of the group rename. The dot becomes a
        hyphen: ``watcher.blobs-1`` would read as the ``-<purpose>`` group form.
        """
        assert CONSUMER_NAME == f"{CONSUMER_GROUP.replace('.', '-')}-1"
        assert CONSUMER_NAME == "watcher-blobs-1"
