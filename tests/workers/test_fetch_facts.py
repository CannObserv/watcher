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

from datetime import UTC, datetime, timedelta

import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.models.changes import BlobAvailableEmit, FetchFailedEmit

from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommandStatus
from src.workers.fetch_facts import process_fact_message
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
        import asyncio
        from contextlib import asynccontextmanager

        import fakeredis
        from co_core.effects.bus import BusPublish
        from co_core.pure.adapters.bus.envelope import to_wire
        from co_core_aio.bus import AsyncBusPublisher

        import src.workers.fetch_facts as ff_mod
        from src.workers.fetch_facts import run_blobs_consumer

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
        pending = await client.xpending(streams.CONTENT_BLOBS, "watcher")
        assert pending["pending"] == 0  # retried message was acked


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
        from co_core.pure.models.changes import RegistryAnnouncementEmit

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
