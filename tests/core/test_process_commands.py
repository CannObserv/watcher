"""Tests for the content.process issue path (#325).

The issuer discipline is ``fetch_commands``'s, against cannobserv#486's
contract:

* a fresh ``command_id`` per (blob, spec) occasion; ``intent_id`` is lineage
  across the spec chain and the reaper's re-issues;
* persist-before-publish — the row commits ``pending_publish`` before any XADD,
  and carries everything the sweep needs to republish from the row alone;
* the command names **one** ``source_spec`` (D3) and the **resolved** dispatch
  essence (cannobserv#486 D1), and ``input_digest`` is the blob fact's bare hex.
"""

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import ContentProcessCommand
from sqlalchemy import text

from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommand
from src.core.models.process_command import ProcessCommand, ProcessCommandStatus
from src.core.notifications.diff_loader import stored_text_location
from src.core.process_commands import (
    DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS,
    DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS,
    PROCESS_COMMAND_HARD_LIMIT_ENV,
    PROCESS_COMMAND_TIMEOUT_ENV,
    PROCESSOR,
    UnsendableProcessCommand,
    chain_process_command,
    create_process_command,
    process_command_hard_limit_seconds,
    process_command_timeout_seconds,
    publish_process_command,
    reissue_process_command,
    select_pending_process_publish,
)
from tests.conftest import make_watched_item

_integration = pytest.mark.integration

NOW = datetime(2026, 10, 2, 16, 0, 0, tzinfo=UTC)
RAW_DIGEST = "61" * 32
SPEC_A = {"extraction": {"selector": "div.content", "algorithm": "css"}, "schema_version": 1}
SPEC_B = {"extraction": {"selector": "main", "algorithm": "css"}, "schema_version": 1}


async def _occasion(db_session, *, specs=(SPEC_A, SPEC_B), **item_kwargs):
    """A watched item and a fetch row holding its blob fact."""
    item_kwargs.setdefault("primary_url", "https://lcb.wa.gov/boardmeetings")
    wi = await make_watched_item(db_session, source_specs=list(specs), **item_kwargs)
    fetch = await create_fetch_command(db_session, wi, now=NOW)
    fetch.content_fingerprint = RAW_DIGEST
    fetch.blob_uri = f"gs://co-gcs-blobs/blobs/{RAW_DIGEST}.bin"
    await db_session.flush()
    return wi, fetch


async def _decode_commands(client):
    entries = await client.xrange(streams.CONTENT_PROCESS)
    decoded = []
    for _message_id, fields in entries:
        frame = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in fields.items()
        }
        decoded.append(from_wire(frame, topic=streams.CONTENT_PROCESS))
    return decoded


class TestKnobs:
    def test_timeout_default(self, monkeypatch):
        monkeypatch.delenv(PROCESS_COMMAND_TIMEOUT_ENV, raising=False)
        assert process_command_timeout_seconds() == DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS

    def test_timeout_override_and_typo_fallback(self, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_TIMEOUT_ENV, "600")
        assert process_command_timeout_seconds() == 600.0
        monkeypatch.setenv(PROCESS_COMMAND_TIMEOUT_ENV, "ten minutes")
        assert process_command_timeout_seconds() == DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS

    def test_hard_limit_default_is_a_day(self, monkeypatch):
        monkeypatch.delenv(PROCESS_COMMAND_HARD_LIMIT_ENV, raising=False)
        assert process_command_hard_limit_seconds() == DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS
        assert DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS == 86400.0

    def test_hard_limit_override(self, monkeypatch):
        monkeypatch.setenv(PROCESS_COMMAND_HARD_LIMIT_ENV, "3600")
        assert process_command_hard_limit_seconds() == 3600.0


@_integration
class TestCreateProcessCommand:
    async def test_persists_pending_publish_row_for_spec_zero(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        await db_session.flush()

        assert row.status == ProcessCommandStatus.PENDING_PUBLISH
        assert row.fetch_command_id == fetch.command_id
        assert row.watched_item_id == wi.id
        assert row.spec_index == 0
        assert row.source_spec == SPEC_A
        assert row.issued_at == NOW
        assert row.published_at is None
        assert row.reissue_count == 0
        assert row.command_id and row.intent_id
        assert row.command_id != fetch.command_id

    async def test_snapshots_what_the_sweep_needs(self, db_session):
        # The sweep holds only this row: the wire's every field comes off it.
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW)

        assert row.info_source_id == wi.archiver_info_source_id
        assert row.input_uri == fetch.blob_uri
        # Bare hex, verbatim from the blob fact — the Emit refuses a prefix.
        assert row.input_digest == RAW_DIGEST

    async def test_a_decisive_lineage_inherits_the_fetch_re_issue_count(self, db_session):
        # The cap is per lineage, and a decisive lineage spans both legs: a
        # fetch already re-issued twice leaves the process leg one turn, not 3.
        wi, fetch = await _occasion(db_session)
        fetch.reissue_count = 2
        row = await create_process_command(
            db_session, fetch, wi, now=NOW, reissue_count=fetch.reissue_count
        )
        assert row.reissue_count == 2

    async def test_resolves_the_dispatch_essence(self, db_session):
        # cannobserv#486 D1: the processor cannot run the URL tiebreaker — its
        # input ends in `.bin` — so the issuer resolves it onto the command.
        wi, fetch = await _occasion(
            db_session,
            primary_url="https://lcb.wa.gov/rules.pdf",
            content_media_type="application/octet-stream",
        )
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        assert row.media_type == "application/pdf"

    async def test_essence_with_nothing_informative_is_none(self, db_session):
        # None is a real resolution (the HTML fallback), stated explicitly.
        wi, fetch = await _occasion(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        assert row.media_type is None

    async def test_fresh_ids_per_occasion(self, db_session):
        wi, fetch = await _occasion(db_session)
        first = await create_process_command(db_session, fetch, wi, now=NOW)
        second = await create_process_command(db_session, fetch, wi, now=NOW)
        assert first.command_id != second.command_id
        assert first.intent_id != second.intent_id

    async def test_prefixed_digest_is_unsendable(self, db_session):
        # Refused at the occasion, not at publish: a row the Emit cannot build
        # would sit pending_publish and fail the sweep every minute, forever.
        wi, fetch = await _occasion(db_session)
        fetch.content_fingerprint = "sha256:" + RAW_DIGEST
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW)

    async def test_no_spec_is_unsendable(self, db_session):
        wi, fetch = await _occasion(db_session, specs=())
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW)

    async def test_no_blob_is_unsendable(self, db_session):
        wi, fetch = await _occasion(db_session)
        fetch.blob_uri = None
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW)

    async def test_cascades_with_the_fetch_row(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        await db_session.flush()
        command_id = row.command_id

        await db_session.delete(fetch)
        await db_session.flush()
        db_session.expire_all()
        assert await db_session.get(ProcessCommand, command_id) is None
        assert await db_session.get(FetchCommand, fetch.command_id) is None


@_integration
class TestSuccessors:
    async def test_chain_runs_the_next_spec_under_the_same_intent(self, db_session):
        wi, fetch = await _occasion(db_session)
        first = await create_process_command(db_session, fetch, wi, now=NOW)
        first.reissue_count = 1
        later = NOW + timedelta(minutes=1)

        nxt = await chain_process_command(db_session, first, SPEC_B, now=later)

        assert nxt.command_id != first.command_id
        assert nxt.intent_id == first.intent_id
        assert nxt.spec_index == 1
        assert nxt.source_spec == SPEC_B
        assert nxt.issued_at == later
        assert nxt.status == ProcessCommandStatus.PENDING_PUBLISH
        # The chain is not a re-issue: the lineage's re-issue count carries.
        assert nxt.reissue_count == 1
        for field in (
            "fetch_command_id",
            "watched_item_id",
            "info_source_id",
            "input_uri",
            "input_digest",
            "media_type",
        ):
            assert getattr(nxt, field) == getattr(first, field), field

    async def test_reissue_repeats_the_spec_and_counts(self, db_session):
        wi, fetch = await _occasion(db_session)
        first = await create_process_command(db_session, fetch, wi, now=NOW)

        again = await reissue_process_command(db_session, first, now=NOW)

        assert again.command_id != first.command_id
        assert again.intent_id == first.intent_id
        assert again.spec_index == first.spec_index
        assert again.source_spec == first.source_spec
        assert again.reissue_count == 1


@_integration
class TestPublishProcessCommand:
    async def test_frame_decodes_as_the_contract_command(self, db_session):
        wi, fetch = await _occasion(
            db_session,
            primary_url="https://lcb.wa.gov/rules.pdf",
            content_media_type="application/octet-stream",
        )
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        client = fakeredis.FakeAsyncRedis()

        await publish_process_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        command = message.payload
        assert isinstance(command, ContentProcessCommand)
        assert command.command_id == row.command_id
        assert command.info_source_id == wi.archiver_info_source_id
        assert command.input_uri == fetch.blob_uri
        assert command.input_digest == RAW_DIGEST
        assert command.processor == PROCESSOR == "extract"
        assert command.source_spec == SPEC_A
        assert command.media_type == "application/pdf"

    async def test_none_media_type_is_stated_on_the_wire(self, db_session):
        wi, fetch = await _occasion(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        client = fakeredis.FakeAsyncRedis()

        await publish_process_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        assert message.payload.media_type is None

    async def test_publish_marks_in_flight(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW)

        await publish_process_command(fakeredis.FakeAsyncRedis(), row, now=NOW)

        assert row.status == ProcessCommandStatus.IN_FLIGHT
        assert row.published_at == NOW

    async def test_select_pending_returns_only_unpublished_oldest_first(self, db_session):
        wi, fetch = await _occasion(db_session)
        late = await create_process_command(db_session, fetch, wi, now=NOW + timedelta(minutes=2))
        early = await create_process_command(db_session, fetch, wi, now=NOW)
        sent = await create_process_command(db_session, fetch, wi, now=NOW)
        await publish_process_command(fakeredis.FakeAsyncRedis(), sent, now=NOW)
        await db_session.flush()

        pending = await select_pending_process_publish(db_session)
        ids = [r.command_id for r in pending]
        assert ids.index(early.command_id) < ids.index(late.command_id)
        assert sent.command_id not in ids


@_integration
class TestSchema:
    """What the table holds once the shadow leg is gone (#350), and #345's index."""

    async def test_the_shadow_columns_are_gone(self, db_session):
        columns = set(
            (
                await db_session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'process_commands'"
                    )
                )
            ).scalars()
        )
        assert columns, "process_commands has no columns — wrong table name?"
        shadow = {
            "local_outcome",
            "local_fingerprint",
            "local_spec_fingerprint",
            "shadow_verdict",
            "shadow_detail",
        }
        assert not columns & shadow

    async def test_the_diff_lookup_has_its_partial_index(self, db_session):
        """#345: ``stored_text_location`` finds a text by ``output_digest``.

        Partial on the lookup's own predicate, so the index holds only rows it
        can return: an answered, stored text.
        """
        indexdef = (
            await db_session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_process_commands_output_digest'"
                )
            )
        ).scalar_one_or_none()
        assert indexdef is not None, "ix_process_commands_output_digest missing"
        assert "(output_digest)" in indexdef
        assert "status" in indexdef and "'completed'" in indexdef
        assert "output_uri IS NOT NULL" in indexdef

    async def test_the_planner_can_answer_the_lookup_from_it(self, db_session):
        """The lookup's predicate implies the index's, so the index is usable.

        Seq scans off, because on a test-sized table the planner rightly
        prefers one; what this pins is that the partial predicate matches.
        """
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW)
        row.status = ProcessCommandStatus.COMPLETED
        row.output_digest = "sha256:" + "aa" * 32
        row.output_uri = "gs://co-gcs-processor/blobs/aa.bin"
        await db_session.flush()

        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            (
                await db_session.execute(
                    text(
                        "EXPLAIN SELECT output_uri, output_size_bytes FROM process_commands "
                        "WHERE output_digest = :fp AND status = 'completed' "
                        "AND output_uri IS NOT NULL "
                        "ORDER BY fact_at DESC NULLS LAST LIMIT 1"
                    ),
                    {"fp": row.output_digest},
                )
            ).scalars()
        )
        assert "ix_process_commands_output_digest" in plan
        stored = await stored_text_location(db_session, row.output_digest)
        assert stored is not None and stored.uri == row.output_uri
