"""The ``info.watch-events`` outbox writers (#369).

Each writer stages one row in the caller's session and commits nothing: the
row must land in the transaction that records its edge, so the commit is the
caller's. The payload is the fact's field map, validated by the drain rather
than here (D2) — a refusal at the edge would roll back the health change it
describes. What these pin is that every writer's payload *is* a valid Emit, so
the drain's validation is a backstop rather than the first check.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from co_core.pure.models.changes import (
    ChangeDetectedEmit,
    WatchErrorEmit,
    WatchRecoveredEmit,
)
from sqlalchemy import select

from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.watch_events import (
    WATCH_EVENT_EMITS,
    enqueue_change_detected,
    enqueue_watch_error,
    enqueue_watch_recovered,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
EPISODE_START = datetime(2026, 10, 18, 9, 30, 0, tzinfo=UTC)
FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64


async def _rows(db_session) -> list[PendingWatchEvent]:
    return list((await db_session.execute(select(PendingWatchEvent))).scalars().all())


class TestWatchError:
    async def test_the_payload_is_a_valid_error_fact(self, db_session):
        wi = await make_watched_item(db_session)

        row = enqueue_watch_error(db_session, wi, now=NOW, reason="http_status", status_code=503)

        emit = WatchErrorEmit.model_validate(row.payload)
        assert emit.event_id == str(row.id)
        assert emit.occurred_at == NOW
        assert emit.info_item_id == str(wi.archiver_info_item_id)
        assert emit.info_source_id == str(wi.archiver_info_source_id)
        assert (emit.reason, emit.status_code) == ("http_status", 503)
        assert row.event_type == "watch_error"
        assert row.info_item_id == str(wi.archiver_info_item_id)

    async def test_an_unbuildable_reason_is_still_staged(self, db_session):
        """D2: the edge records what it has; the drain is where a payload the
        Emit refuses dead-letters, never the commit that holds the health."""
        wi = await make_watched_item(db_session)

        row = enqueue_watch_error(db_session, wi, now=NOW, reason=None, status_code=None)

        assert row.payload["reason"] is None


class TestWatchRecovered:
    async def test_carries_the_episode_start(self, db_session):
        wi = await make_watched_item(db_session)

        row = enqueue_watch_recovered(db_session, wi, now=NOW, error_since=EPISODE_START)

        emit = WatchRecoveredEmit.model_validate(row.payload)
        assert emit.error_since == EPISODE_START
        assert emit.event_id == str(row.id)
        assert row.event_type == "watch_recovered"

    async def test_an_unknown_start_is_stated_as_null_never_omitted(self, db_session):
        """Required but nullable (contract (c)): an episode opened before the
        column existed recovers with ``null`` in the payload."""
        wi = await make_watched_item(db_session)

        row = enqueue_watch_recovered(db_session, wi, now=NOW, error_since=None)

        assert "error_since" in row.payload
        assert row.payload["error_since"] is None
        assert WatchRecoveredEmit.model_validate(row.payload).error_since is None


class TestChangeDetected:
    async def test_the_payload_is_a_valid_change_fact(self, db_session):
        wi = await make_watched_item(db_session)

        row = enqueue_change_detected(
            db_session,
            wi,
            now=NOW,
            previous_fingerprint=FP_A,
            current_fingerprint=FP_B,
            previous_text_uri=None,
            current_text_uri="gs://co-gcs-texts/texts/b.txt",
            extraction_changed="spec",
            previous_changed_at=EPISODE_START,
        )

        emit = ChangeDetectedEmit.model_validate(row.payload)
        assert (emit.previous_fingerprint, emit.current_fingerprint) == (FP_A, FP_B)
        assert emit.previous_text_uri is None
        assert emit.current_text_uri == "gs://co-gcs-texts/texts/b.txt"
        assert emit.extraction_changed == "spec"
        assert emit.previous_changed_at == EPISODE_START
        assert "previous_text_uri" in row.payload  # required-but-nullable: stated
        assert row.event_type == "change_detected"


class TestStaging:
    async def test_writers_stage_and_never_commit(self, db_session, monkeypatch):
        """The edge's own commit carries the row; a writer that committed would
        split the fact from the state change it reports."""
        wi = await make_watched_item(db_session)
        commit = AsyncMock()
        monkeypatch.setattr(db_session, "commit", commit)
        flush = AsyncMock()
        monkeypatch.setattr(db_session, "flush", flush)

        row = enqueue_watch_error(db_session, wi, now=NOW, reason="fetch_timeout", status_code=None)

        commit.assert_not_awaited()
        flush.assert_not_awaited()
        monkeypatch.undo()
        assert row in db_session.new
        await db_session.flush()
        assert [r.id for r in await _rows(db_session)] == [row.id]

    async def test_every_staged_kind_has_an_emit(self, db_session):
        """The drain picks the class by ``event_type``; a writer whose kind is
        missing from the map would dead-letter every row it stages."""
        wi = await make_watched_item(db_session)
        staged = [
            enqueue_watch_error(db_session, wi, now=NOW, reason="fetch_timeout", status_code=None),
            enqueue_watch_recovered(db_session, wi, now=NOW, error_since=None),
            enqueue_change_detected(
                db_session,
                wi,
                now=NOW,
                previous_fingerprint=FP_A,
                current_fingerprint=FP_B,
                previous_text_uri=None,
                current_text_uri="gs://t/b",
                extraction_changed=None,
                previous_changed_at=None,
            ),
        ]
        for row in staged:
            WATCH_EVENT_EMITS[row.event_type].model_validate(row.payload)

    async def test_each_fact_mints_its_own_event_id(self, db_session):
        wi = await make_watched_item(db_session)

        first = enqueue_watch_error(db_session, wi, now=NOW, reason="a", status_code=None)
        second = enqueue_watch_error(db_session, wi, now=NOW, reason="a", status_code=None)

        assert first.payload["event_id"] != second.payload["event_id"]
