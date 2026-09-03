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
        import asyncio
        from contextlib import asynccontextmanager

        import fakeredis
        from co_core.effects.bus import BusPublish
        from co_core.pure.adapters.bus.envelope import to_wire
        from co_core_aio.bus import AsyncBusPublisher

        import src.workers.fetch_facts as ff_mod
        from src.workers.fetch_facts import CONSUMER_GROUP, run_blobs_consumer

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
        from co_core.pure.adapters.bus.streams import group_name

        from src.workers.fetch_facts import CONSUMER_GROUP

        assert CONSUMER_GROUP == group_name(streams.CONTENT_BLOBS, "watcher")
        assert CONSUMER_GROUP == "watcher.blobs"

    def test_no_purpose_segment_for_a_lone_group(self):
        """Watcher runs exactly one group on content.blobs, so the segment that
        disambiguates two groups of the same service must be absent."""
        from src.workers.fetch_facts import CONSUMER_GROUP

        assert CONSUMER_GROUP.count(".") == 1
        assert "-" not in CONSUMER_GROUP

    def test_consumer_name_stays_group_derived_and_host_independent(self):
        """The audit's conclusion on the *name* was 'verified, nothing to do' —
        it changes only as a consequence of the group rename. The dot becomes a
        hyphen: ``watcher.blobs-1`` would read as the ``-<purpose>`` group form.
        """
        from src.workers.fetch_facts import CONSUMER_GROUP, CONSUMER_NAME

        assert CONSUMER_NAME == f"{CONSUMER_GROUP.replace('.', '-')}-1"
        assert CONSUMER_NAME == "watcher-blobs-1"


class TestLegacyGroupMigration:
    """#285 — the rename carries a silent-loss hazard, so Watcher performs it.

    ``ensure_group(start_id='$')`` mints at the tail. A renamed Watcher that
    restarts before the broker-side ``XGROUP CREATE`` therefore drops everything
    published between the last ``watcher`` read and that moment, with no error,
    no PEL and no signal. Enforcing the create-before-read ordering in-process
    turns a hand-typed runbook step into a tested path — and decouples the
    rename from the CannObserv/broker#1 Phase 3 window.
    """

    async def _legacy_group_at(self, client, *, read: int):
        """Legacy group advanced past ``read`` entries, all acked (prod's shape)."""
        await client.xgroup_create(streams.CONTENT_BLOBS, "watcher", id="0", mkstream=True)
        if read:
            batches = await client.xreadgroup(
                "watcher", "watcher-1", {streams.CONTENT_BLOBS: ">"}, count=read
            )
            for _stream, entries in batches:
                for message_id, _fields in entries:
                    await client.xack(streams.CONTENT_BLOBS, "watcher", message_id)

    async def _group_names(self, client):
        groups = await client.xinfo_groups(streams.CONTENT_BLOBS)
        return {g["name"].decode() if isinstance(g["name"], bytes) else g["name"] for g in groups}

    async def test_new_group_inherits_the_legacy_position(self):
        """The whole point: facts the legacy group had not yet read must still
        be delivered, and ones it had already acked must not be re-delivered."""
        import fakeredis

        from src.workers.fetch_facts import CONSUMER_GROUP, migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "1"})
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "2"})
        await self._legacy_group_at(client, read=2)
        unread = await client.xadd(streams.CONTENT_BLOBS, {"seq": "3"})

        assert await migrate_legacy_group(client) == "migrated"

        batches = await client.xreadgroup(
            CONSUMER_GROUP, "watcher-blobs-1", {streams.CONTENT_BLOBS: ">"}, count=10
        )
        delivered = [message_id for _stream, entries in batches for message_id, _f in entries]
        assert delivered == [unread]

    async def test_legacy_group_is_destroyed_once_drained(self):
        import fakeredis

        from src.workers.fetch_facts import CONSUMER_GROUP, migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "1"})
        await self._legacy_group_at(client, read=1)

        await migrate_legacy_group(client)

        assert await self._group_names(client) == {CONSUMER_GROUP}

    async def test_undrained_pel_keeps_both_groups(self):
        """Destroying a group with a live PEL discards the entries in it. The
        audit read ``pending 0``; that is a reading, not a property, so the
        drop is conditional and the mismatch is loud rather than silent."""
        import fakeredis

        from src.workers.fetch_facts import CONSUMER_GROUP, migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "1"})
        await client.xgroup_create(streams.CONTENT_BLOBS, "watcher", id="0", mkstream=True)
        await client.xreadgroup("watcher", "watcher-1", {streams.CONTENT_BLOBS: ">"}, count=1)

        assert await migrate_legacy_group(client) == "legacy_pel_not_drained"
        assert await self._group_names(client) == {"watcher", CONSUMER_GROUP}

    async def test_absent_legacy_group_is_a_noop(self):
        """Greenfield and every boot after the first: nothing to rename, and no
        group created here — ``ensure_group`` owns that."""
        import fakeredis

        from src.workers.fetch_facts import migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "1"})

        assert await migrate_legacy_group(client) == "no_legacy_group"
        assert await self._group_names(client) == set()

    async def test_missing_stream_is_a_noop(self):
        """A broker with no content.blobs at all — a fresh deployment."""
        import fakeredis

        from src.workers.fetch_facts import migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()

        assert await migrate_legacy_group(client) == "no_legacy_group"

    async def test_second_call_is_idempotent(self):
        import fakeredis

        from src.workers.fetch_facts import CONSUMER_GROUP, migrate_legacy_group

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.CONTENT_BLOBS, {"seq": "1"})
        await self._legacy_group_at(client, read=1)

        assert await migrate_legacy_group(client) == "migrated"
        assert await migrate_legacy_group(client) == "no_legacy_group"
        assert await self._group_names(client) == {CONSUMER_GROUP}

    async def test_consumer_loop_migrates_before_it_reads(self, db_session):
        """End to end: a fact published while the old-named Watcher was down is
        still processed after the rename. Under a bare ``ensure_group('$')`` it
        would be dropped with no signal at all."""
        import asyncio
        from contextlib import asynccontextmanager

        import fakeredis
        from co_core.effects.bus import BusPublish
        from co_core_aio.bus import AsyncBusPublisher

        from src.workers.fetch_facts import CONSUMER_GROUP, run_blobs_consumer

        client = fakeredis.FakeAsyncRedis()
        await client.xgroup_create(streams.CONTENT_BLOBS, "watcher", id="0", mkstream=True)
        # Published in the gap: after the last `watcher` read, before the restart.
        event = _blob_message("01UNKNOWNCOMMANDIDXXXXXXXX").payload
        await AsyncBusPublisher(client).execute(BusPublish(streams.CONTENT_BLOBS, to_wire(event)))

        stop = asyncio.Event()

        @asynccontextmanager
        async def _ctx():
            yield db_session

        task = asyncio.create_task(
            run_blobs_consumer(
                client, lambda: _ctx(), stop=stop, block_ms=10, error_backoff_seconds=0.01
            )
        )

        async def _until_acked():
            while True:
                info = await client.xinfo_groups(streams.CONTENT_BLOBS)
                for group in info:
                    name = group["name"]
                    name = name.decode() if isinstance(name, bytes) else name
                    if name == CONSUMER_GROUP and group["entries-read"]:
                        return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_until_acked(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        pending = await client.xpending(streams.CONTENT_BLOBS, CONSUMER_GROUP)
        assert pending["pending"] == 0  # the gap fact was delivered and acked
