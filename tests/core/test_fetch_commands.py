"""Tests for the content.fetch issue path (#241, Phase 4 step 1).

Pins the issuer-contract MUSTs that live on this side of the wire:

* MUST-1 — a fresh ``command_id`` per fetch *occasion*: two issues for one item
  mint two ids (a resource-stable id would make every re-fetch inside
  Replicator's dedupe TTL a silent no-op — precisely Watcher's job).
* MUST-2 — persist-before-publish: the row commits ``pending_publish`` before
  any XADD; the sweep republishes a crashed publish **under the same id**
  (idempotent by Replicator's dedupe).
* replicator#11 — the command carries the pinned watcher User-Agent, so the
  cutover is UA-neutral and fingerprints stay byte-continuous.
"""

import logging
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import ContentFetchCommand

from src.core.fetch_commands import (
    DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_FETCH_MAX_REISSUES,
    FETCH_COMMAND_TIMEOUT_ENV,
    FETCH_MAX_REISSUES_ENV,
    WATCHER_USER_AGENT,
    create_fetch_command,
    fetch_command_timeout_seconds,
    fetch_max_reissues,
    publish_fetch_command,
)
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from src.core.validators import CONDITIONAL_GET_ENV, validator_source_key
from tests.conftest import make_watched_item

# The mark sits on the classes that need PostgreSQL, not on the module: the
# env-knob tests below are pure reads and belong in the default suite (CR-14).
_integration = pytest.mark.integration

NOW = datetime(2026, 8, 6, 16, 0, 0, tzinfo=UTC)


async def _decode_commands(client):
    entries = await client.xrange(streams.CONTENT_FETCH)
    decoded = []
    for _message_id, fields in entries:
        frame = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in fields.items()
        }
        decoded.append(from_wire(frame, topic=streams.CONTENT_FETCH))
    return decoded


@_integration
class TestCreateFetchCommand:
    async def test_persists_pending_publish_row(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        assert row.status == FetchCommandStatus.PENDING_PUBLISH
        assert row.url == "https://lcb.wa.gov/notices"
        assert row.watched_item_id == wi.id
        assert row.issued_at == NOW
        assert row.published_at is None
        assert row.reissue_count == 0
        assert row.command_id and row.intent_id

    async def test_fresh_command_id_per_occasion(self, db_session):
        # MUST-1: never resource-stable, never per-run.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        first = await create_fetch_command(db_session, wi, now=NOW)
        second = await create_fetch_command(db_session, wi, now=NOW)
        assert first.command_id != second.command_id
        assert first.intent_id != second.intent_id

    async def test_snapshots_the_info_source_id(self, db_session):
        # #252/cannobserv#300: the domain key rides on the row, so the sweep —
        # which holds no WatchedItem — can still publish a valid command.
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        assert row.info_source_id == wi.archiver_info_source_id

    async def test_reissue_keeps_intent_lineage(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        first = await create_fetch_command(db_session, wi, now=NOW)
        reissued = await create_fetch_command(
            db_session, wi, now=NOW, intent_id=first.intent_id, reissue_count=1
        )
        assert reissued.command_id != first.command_id
        assert reissued.intent_id == first.intent_id
        assert reissued.reissue_count == 1


@_integration
class TestPublishFetchCommand:
    async def test_frame_decodes_with_pinned_user_agent(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        client = fakeredis.FakeAsyncRedis()

        await publish_fetch_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        command = message.payload
        assert isinstance(command, ContentFetchCommand)
        assert command.command_id == row.command_id
        assert command.url == row.url
        # replicator#11: UA-neutral cutover — fingerprint byte-continuity.
        assert command.headers == {"user-agent": WATCHER_USER_AGENT}
        # cannobserv#300: the fetch names the domain object it is for.
        assert command.info_source_id == wi.archiver_info_source_id

    async def test_publish_marks_in_flight(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        client = fakeredis.FakeAsyncRedis()

        await publish_fetch_command(client, row, now=NOW)

        assert row.status == FetchCommandStatus.IN_FLIGHT
        assert row.published_at == NOW

    async def test_cascade_on_watched_item_delete(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        command_id = row.command_id

        await db_session.delete(wi)
        await db_session.flush()
        # The cascade happens DB-side; drop the identity-map copy before re-reading.
        db_session.expire_all()
        assert await db_session.get(FetchCommand, command_id) is None


@_integration
class TestValidatorReplay:
    """#269 part 3: the command replays the item's stored validators.

    Snapshotted onto the row at issue, not read from the item at publish: the
    pending-publish sweep holds only the row (the same argument that put
    ``info_source_id`` there, cannobserv#300), and the row is then an exact
    record of what each occasion asked.
    """

    async def _item_with_validators(self, db_session, **over):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        wi.etag = over.pop("etag", 'W/"v2"')
        wi.last_modified = over.pop("last_modified", "Wed, 13 Aug 2026 10:00:00 GMT")
        wi.last_full_fetch_at = over.pop("last_full_fetch_at", NOW - timedelta(hours=1))
        wi.validator_source_key = over.pop(
            "validator_source_key",
            validator_source_key(effective_url=wi.effective_url, source_specs=wi.source_specs),
        )
        await db_session.flush()
        return wi

    async def test_snapshots_the_pair_onto_the_row(self, db_session, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session)

        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        assert row.request_etag == 'W/"v2"'
        assert row.request_last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"

    async def test_publishes_the_validator_headers_verbatim(self, db_session, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session)
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        client = fakeredis.FakeAsyncRedis()

        await publish_fetch_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        command = message.payload
        assert command.headers == {
            "user-agent": WATCHER_USER_AGENT,
            "if-none-match": 'W/"v2"',
            "if-modified-since": "Wed, 13 Aug 2026 10:00:00 GMT",
        }

    async def test_the_gate_off_sends_only_the_user_agent(self, db_session, monkeypatch):
        # Default posture, and the canary's off-position: byte-for-byte the
        # pre-#269 command.
        monkeypatch.delenv(CONDITIONAL_GET_ENV, raising=False)
        wi = await self._item_with_validators(db_session)
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        client = fakeredis.FakeAsyncRedis()

        await publish_fetch_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        command = message.payload
        assert command.headers == {"user-agent": WATCHER_USER_AGENT}
        assert row.request_etag is None

    async def test_force_full_fetch_snapshots_nothing(self, db_session, monkeypatch):
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session)

        row = await create_fetch_command(db_session, wi, now=NOW, force_full_fetch=True)
        await db_session.flush()

        assert row.request_etag is None
        assert row.request_last_modified is None

    async def test_an_unsendable_stored_value_is_never_snapshotted(self, db_session, monkeypatch):
        # Replicator refuses it BEFORE any request goes out, so minting the
        # command at all buys an ERROR health transition for nothing.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session, etag='"v2"\r\nX-Evil: 1')

        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        assert row.request_etag is None
        assert row.request_last_modified == "Wed, 13 Aug 2026 10:00:00 GMT"

    async def test_a_stale_source_key_snapshots_nothing(self, db_session, monkeypatch):
        # The specs, the URL, or the extraction generation moved: the pair was
        # earned under a different meaning of the bytes.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session, validator_source_key="sha256:stale")

        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()

        assert row.request_etag is None

    async def test_the_sweep_republishes_the_same_headers(self, db_session, monkeypatch):
        # The sweep holds only the row — the snapshot is what makes its
        # republish byte-identical to the original command.
        monkeypatch.setenv(CONDITIONAL_GET_ENV, "true")
        wi = await self._item_with_validators(db_session)
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        monkeypatch.delenv(CONDITIONAL_GET_ENV, raising=False)
        client = fakeredis.FakeAsyncRedis()

        await publish_fetch_command(client, row, now=NOW)

        (message,) = await _decode_commands(client)
        command = message.payload
        assert command.headers["if-none-match"] == 'W/"v2"'


class TestFetchMaxReissues:
    def test_defaults_to_three(self, monkeypatch):
        monkeypatch.delenv(FETCH_MAX_REISSUES_ENV, raising=False)
        assert fetch_max_reissues() == DEFAULT_FETCH_MAX_REISSUES

    def test_reads_the_env_override(self, monkeypatch):
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "5")
        assert fetch_max_reissues() == 5

    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch, caplog):
        # CR-1: this is read inside ``except BlobUnreadable`` in the blob apply.
        # Raising there escapes the handler, leaves the row IN_FLIGHT holding a
        # fact, and the reaper re-defers the same doomed apply every window —
        # the unbounded loop #275 removed, in env-var form.
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "three")
        with caplog.at_level(logging.WARNING, logger="src.core.fetch_commands"):
            assert fetch_max_reissues() == DEFAULT_FETCH_MAX_REISSUES
        assert any(FETCH_MAX_REISSUES_ENV in r.getMessage() for r in caplog.records)

    def test_zero_is_honoured_as_no_reissues(self, monkeypatch):
        # Safe direction — an unreadable blob fails on the first occasion — so
        # it passes through rather than being corrected to the default.
        monkeypatch.setenv(FETCH_MAX_REISSUES_ENV, "0")
        assert fetch_max_reissues() == 0


class TestFetchCommandTimeoutSeconds:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv(FETCH_COMMAND_TIMEOUT_ENV, raising=False)
        assert fetch_command_timeout_seconds() == DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS

    def test_a_non_positive_timeout_is_logged(self, monkeypatch, caplog):
        # CR-13: safe direction it is not — every in-flight command becomes
        # stale at once, so one reaper pass re-issues the fleet. Say it out loud.
        monkeypatch.setenv(FETCH_COMMAND_TIMEOUT_ENV, "0")
        with caplog.at_level(logging.INFO, logger="src.core.fetch_commands"):
            assert fetch_command_timeout_seconds() == 0.0
        assert any("not positive" in r.getMessage() for r in caplog.records)

    def test_reads_the_env_override(self, monkeypatch):
        monkeypatch.setenv(FETCH_COMMAND_TIMEOUT_ENV, "60")
        assert fetch_command_timeout_seconds() == 60.0

    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch, caplog):
        # CR-3: worse blast radius than the cap — this one is read once per
        # reaper pass, so a typo takes out the whole sweep, not one item.
        monkeypatch.setenv(FETCH_COMMAND_TIMEOUT_ENV, "half an hour")
        with caplog.at_level(logging.WARNING, logger="src.core.fetch_commands"):
            assert fetch_command_timeout_seconds() == DEFAULT_FETCH_COMMAND_TIMEOUT_SECONDS
        assert any(FETCH_COMMAND_TIMEOUT_ENV in r.getMessage() for r in caplog.records)
