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

import logging
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import ContentProcessCommand

from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommand
from src.core.models.process_command import (
    LocalOutcome,
    ProcessCommand,
    ProcessCommandStatus,
    ShadowVerdict,
)
from src.core.process_commands import (
    DEFAULT_PROCESS_COMMAND_HARD_LIMIT_SECONDS,
    DEFAULT_PROCESS_COMMAND_TIMEOUT_SECONDS,
    EXTRACT_MODE_ENV,
    PROCESS_COMMAND_HARD_LIMIT_ENV,
    PROCESS_COMMAND_TIMEOUT_ENV,
    PROCESSOR,
    ExtractMode,
    LocalExtraction,
    UnsendableProcessCommand,
    chain_process_command,
    create_process_command,
    extract_mode,
    judge_shadow,
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
LOCAL = LocalExtraction(
    outcome=LocalOutcome.UNCHANGED,
    fingerprint="sha256:" + "ab" * 32,
    spec_fingerprint="spec1:sha256:" + "cd" * 32,
)


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


class TestExtractMode:
    def test_defaults_to_local(self, monkeypatch):
        monkeypatch.delenv(EXTRACT_MODE_ENV, raising=False)
        assert extract_mode() is ExtractMode.LOCAL

    @pytest.mark.parametrize("raw", ["shadow", "SHADOW", " shadow "])
    def test_reads_shadow(self, monkeypatch, raw):
        monkeypatch.setenv(EXTRACT_MODE_ENV, raw)
        assert extract_mode() is ExtractMode.SHADOW

    @pytest.mark.parametrize("raw", ["processor", "observo", "shdaow", ""])
    def test_unknown_value_falls_back_to_local_loudly(self, monkeypatch, caplog, raw):
        # A knob must not wedge the path it governs: an unrecognised value — the
        # switch's own `processor` included, until #326 builds it — keeps local
        # extraction deciding, and says so.
        monkeypatch.setenv(EXTRACT_MODE_ENV, raw)
        with caplog.at_level(logging.WARNING):
            assert extract_mode() is ExtractMode.LOCAL
        assert EXTRACT_MODE_ENV in caplog.text


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
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
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
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

        assert row.info_source_id == wi.archiver_info_source_id
        assert row.input_uri == fetch.blob_uri
        # Bare hex, verbatim from the blob fact — the Emit refuses a prefix.
        assert row.input_digest == RAW_DIGEST

    async def test_carries_the_local_answer(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

        assert row.local_outcome == LocalOutcome.UNCHANGED
        assert row.local_fingerprint == LOCAL.fingerprint
        assert row.local_spec_fingerprint == LOCAL.spec_fingerprint

    async def test_resolves_the_dispatch_essence(self, db_session):
        # cannobserv#486 D1: the processor cannot run the URL tiebreaker — its
        # input ends in `.bin` — so the issuer resolves it onto the command.
        wi, fetch = await _occasion(
            db_session,
            primary_url="https://lcb.wa.gov/rules.pdf",
            content_media_type="application/octet-stream",
        )
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        assert row.media_type == "application/pdf"

    async def test_essence_with_nothing_informative_is_none(self, db_session):
        # None is a real resolution (the HTML fallback), stated explicitly.
        wi, fetch = await _occasion(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        assert row.media_type is None

    async def test_fresh_ids_per_occasion(self, db_session):
        wi, fetch = await _occasion(db_session)
        first = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        second = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        assert first.command_id != second.command_id
        assert first.intent_id != second.intent_id

    async def test_prefixed_digest_is_unsendable(self, db_session):
        # Refused at the occasion, not at publish: a row the Emit cannot build
        # would sit pending_publish and fail the sweep every minute, forever.
        wi, fetch = await _occasion(db_session)
        fetch.content_fingerprint = "sha256:" + RAW_DIGEST
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

    async def test_no_spec_is_unsendable(self, db_session):
        wi, fetch = await _occasion(db_session, specs=())
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

    async def test_no_blob_is_unsendable(self, db_session):
        wi, fetch = await _occasion(db_session)
        fetch.blob_uri = None
        with pytest.raises(UnsendableProcessCommand):
            await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

    async def test_cascades_with_the_fetch_row(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
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
        first = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
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
            "local_outcome",
            "local_fingerprint",
            "local_spec_fingerprint",
        ):
            assert getattr(nxt, field) == getattr(first, field), field

    async def test_reissue_repeats_the_spec_and_counts(self, db_session):
        wi, fetch = await _occasion(db_session)
        first = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

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
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
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
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        client = fakeredis.FakeAsyncRedis()

        await publish_process_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        assert message.payload.media_type is None

    async def test_publish_marks_in_flight(self, db_session):
        wi, fetch = await _occasion(db_session)
        row = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)

        await publish_process_command(fakeredis.FakeAsyncRedis(), row, now=NOW)

        assert row.status == ProcessCommandStatus.IN_FLIGHT
        assert row.published_at == NOW

    async def test_select_pending_returns_only_unpublished_oldest_first(self, db_session):
        wi, fetch = await _occasion(db_session)
        late = await create_process_command(
            db_session, fetch, wi, now=NOW + timedelta(minutes=2), local=LOCAL
        )
        early = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        sent = await create_process_command(db_session, fetch, wi, now=NOW, local=LOCAL)
        await publish_process_command(fakeredis.FakeAsyncRedis(), sent, now=NOW)
        await db_session.flush()

        pending = await select_pending_process_publish(db_session)
        ids = [r.command_id for r in pending]
        assert ids.index(early.command_id) < ids.index(late.command_id)
        assert sent.command_id not in ids


LOCAL_DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "ef" * 32
LOCAL_SPEC = "spec1:sha256:" + "cd" * 32


def _settled(*, local_fingerprint=LOCAL_DIGEST, local_spec=LOCAL_SPEC, **fact) -> ProcessCommand:
    """An in-memory row as the consumer leaves it; no database needed to judge it."""
    outcome = LocalOutcome.EXTRACTION_FAILED if local_fingerprint is None else LocalOutcome.CHANGED
    return ProcessCommand(
        local_outcome=outcome,
        local_fingerprint=local_fingerprint,
        local_spec_fingerprint=local_spec if local_fingerprint else None,
        **fact,
    )


def _complete(**over):
    fact = {
        "status": ProcessCommandStatus.COMPLETED,
        "empty": False,
        "output_digest": LOCAL_DIGEST,
        "spec_fingerprint": LOCAL_SPEC,
    }
    return {**fact, **over}


def _failed(reason, **over):
    return {"status": ProcessCommandStatus.FAILED, "failure_reason": reason, **over}


class TestJudgeShadow:
    """The comparator: the processor's answer against local extraction's, per lineage.

    The switch (#326) is gated on zero mismatches, so a mismatch must mean the
    two extractors disagreed about the same bytes — and nothing else may be
    allowed to read as agreement.
    """

    def test_equal_digest_and_spec_is_a_match(self):
        assert judge_shadow(_settled(**_complete())) == (ShadowVerdict.MATCH, None)

    def test_different_digest_is_a_mismatch(self):
        verdict, detail = judge_shadow(_settled(**_complete(output_digest=OTHER_DIGEST)))
        assert verdict is ShadowVerdict.MISMATCH
        assert LOCAL_DIGEST in detail and OTHER_DIGEST in detail

    def test_different_spec_is_a_mismatch_even_with_equal_digest(self):
        # The fallback loop bound a different spec than local's did: the chain
        # disagrees with the loop it replaces, whatever the bytes say.
        verdict, detail = judge_shadow(_settled(**_complete(spec_fingerprint="spec1:other")))
        assert verdict is ShadowVerdict.MISMATCH
        assert "spec" in detail

    def test_unknown_spec_identity_on_either_side_judges_the_digest_alone(self):
        # co-core reports None when the derivation raises; unknown is not a disagreement.
        assert judge_shadow(_settled(**_complete(spec_fingerprint=None)))[0] is ShadowVerdict.MATCH
        assert judge_shadow(_settled(local_spec=None, **_complete()))[0] is ShadowVerdict.MATCH

    def test_processor_derived_text_where_local_failed_is_a_mismatch(self):
        verdict, _ = judge_shadow(_settled(local_fingerprint=None, **_complete()))
        assert verdict is ShadowVerdict.MISMATCH

    def test_empty_on_the_last_spec_matches_a_local_failure(self):
        # #258: local raises on all-empty; the chain ends on an empty outcome.
        row = _settled(local_fingerprint=None, **_complete(empty=True, output_digest=None))
        assert judge_shadow(row) == (ShadowVerdict.MATCH, None)

    def test_empty_on_the_last_spec_where_local_derived_text_is_a_mismatch(self):
        row = _settled(**_complete(empty=True, output_digest=None))
        assert judge_shadow(row)[0] is ShadowVerdict.MISMATCH

    def test_extraction_error_matches_a_local_failure(self):
        row = _settled(local_fingerprint=None, **_failed("extraction_error"))
        assert judge_shadow(row) == (ShadowVerdict.MATCH, None)

    def test_extraction_error_where_local_derived_text_is_a_mismatch(self):
        # Includes processor#17's give-up (`dead-lettered: …` in detail): a
        # processor that cannot finish what local finished blocks the switch.
        row = _settled(**_failed("extraction_error", failure_detail="dead-lettered: boom"))
        verdict, detail = judge_shadow(row)
        assert verdict is ShadowVerdict.MISMATCH
        assert "extraction_error" in detail

    @pytest.mark.parametrize(
        "reason",
        [
            "input_unreadable",
            "invalid_input",
            "input_digest_mismatch",
            "unsupported_media_type",
            "unsupported_processor",
            "a_reason_from_the_future",
        ],
    )
    def test_input_and_plumbing_failures_are_uncompared(self, reason):
        # The processor never judged the bytes: coverage lost, not disagreement.
        verdict, detail = judge_shadow(_settled(**_failed(reason)))
        assert verdict is ShadowVerdict.UNCOMPARED
        assert detail == reason

    def test_a_row_without_a_local_answer_is_uncompared(self):
        row = ProcessCommand(**_complete())
        assert judge_shadow(row)[0] is ShadowVerdict.UNCOMPARED
