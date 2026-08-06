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

from datetime import UTC, datetime

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import ContentFetchCommand

from src.core.fetch import WATCHER_USER_AGENT
from src.core.fetch_commands import (
    create_fetch_command,
    publish_fetch_command,
)
from src.core.models.fetch_command import FetchCommand, FetchCommandStatus
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

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

    async def test_reissue_keeps_intent_lineage(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        first = await create_fetch_command(db_session, wi, now=NOW)
        reissued = await create_fetch_command(
            db_session, wi, now=NOW, intent_id=first.intent_id, reissue_count=1
        )
        assert reissued.command_id != first.command_id
        assert reissued.intent_id == first.intent_id
        assert reissued.reissue_count == 1


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
