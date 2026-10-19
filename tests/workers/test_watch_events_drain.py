"""The ``info.watch-events`` producer: drains ``pending_watch_events`` (#369).

Two properties beyond the ``content.revisions`` drain's failure split:

* **Head-of-line order.** Facts reach the stream in the order their edges
  committed, so a recovery can never land before the error edge it closes. A
  pass stops at the first row it cannot send — a transient publish failure, or
  a row still serving its backoff — and the rows behind it wait.
* **A refused payload is skipped past, not waited on.** Building is pure, so a
  payload the Emit refuses dead-letters at once and the next row publishes.
"""

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import WatchErrorEvent, WatchRecoveredEvent
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError, ResponseError
from sqlalchemy import select

import src.workers.watch_events_drain as drain_mod
from src.core.models.pending_watch_event import PendingWatchEvent
from src.core.watch_events import enqueue_watch_error, enqueue_watch_recovered
from src.workers.watch_events_drain import MAX_PUBLISH_ATTEMPTS, drain_pending_watch_events
from tests.conftest import make_watched_item
from tests.workers.bus_helpers import mock_session_factory, refusing_client

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def _wire(db_session, monkeypatch):
    monkeypatch.setattr(drain_mod, "get_session_factory", lambda: mock_session_factory(db_session))


async def _entries(client) -> list:
    out = []
    for message_id, raw in await client.xrange(streams.INFO_WATCH_EVENTS):
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        message = from_wire(fields, topic=streams.INFO_WATCH_EVENTS, message_id=message_id.decode())
        out.append((fields["key"], message.payload))
    return out


async def _rows(db_session) -> list[PendingWatchEvent]:
    stmt = select(PendingWatchEvent).order_by(PendingWatchEvent.id)
    return list((await db_session.execute(stmt)).scalars().all())


async def _episode(db_session):
    """An item's error edge and its recovery, staged in that order."""
    wi = await make_watched_item(db_session)
    error = enqueue_watch_error(db_session, wi, now=NOW, reason="http_status", status_code=503)
    recovered = enqueue_watch_recovered(
        db_session, wi, now=NOW + timedelta(hours=1), error_since=NOW
    )
    await db_session.commit()
    return wi, error, recovered


class TestPublish:
    async def test_publishes_each_fact_keyed_on_its_event_id(self, db_session, monkeypatch):
        _wi, error, recovered = await _episode(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_watch_events(bus_client=client)

        assert result["published"] == 2
        (k1, p1), (k2, p2) = await _entries(client)
        assert isinstance(p1, WatchErrorEvent) and isinstance(p2, WatchRecoveredEvent)
        assert (k1, k2) == (str(error.id), str(recovered.id))
        assert (p1.reason, p1.status_code) == ("http_status", 503)
        assert p2.error_since == NOW
        assert await _rows(db_session) == []

    async def test_a_redelivery_carries_the_stored_event_id(self, db_session, monkeypatch):
        """A crash between XADD and commit republishes the row: the key is the
        one minted at the edge, so the consumer's dedupe sees one fact."""
        wi = await make_watched_item(db_session)
        row = enqueue_watch_error(db_session, wi, now=NOW, reason="fetch_timeout", status_code=None)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        monkeypatch.setattr(db_session, "delete", _noop_delete)

        await drain_pending_watch_events(bus_client=client)
        _wire(db_session, monkeypatch)
        await drain_pending_watch_events(bus_client=client)

        keys = [key for key, _ in await _entries(client)]
        assert keys == [str(row.id), str(row.id)]

    async def test_publishes_with_no_length_cap(self, db_session, monkeypatch):
        """A fact stream: a cap deletes facts a group has not read (#327)."""
        await _episode(db_session)
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        calls = []
        original = client.xadd

        async def spy(*args, **kwargs):
            calls.append(kwargs)
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "xadd", spy)

        await drain_pending_watch_events(bus_client=client)

        assert calls and all(kw.get("maxlen") is None for kw in calls)


async def _noop_delete(_row):
    return None


class TestOrdering:
    async def test_a_transient_failure_holds_back_the_rows_behind(self, db_session, monkeypatch):
        """The recovery must not reach the stream before its error edge."""
        _wi, error, recovered = await _episode(db_session)
        _wire(db_session, monkeypatch)
        client = refusing_client(RedisConnectionError("connection refused"))

        result = await drain_pending_watch_events(bus_client=client)

        assert result == {"published": 0, "failed": 1, "dead_lettered": 0}
        assert client.xadd.await_count == 1  # the head only
        await db_session.refresh(error)
        await db_session.refresh(recovered)
        assert error.attempts == 1 and error.next_attempt_at > datetime.now(UTC)
        assert recovered.attempts == 0

    async def test_a_backed_off_head_holds_the_queue(self, db_session, monkeypatch):
        _wi, error, recovered = await _episode(db_session)
        error.attempts = 1
        error.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_watch_events(bus_client=client)

        assert result["published"] == 0
        assert await client.xlen(streams.INFO_WATCH_EVENTS) == 0

    async def test_once_the_head_publishes_the_rest_follow(self, db_session, monkeypatch):
        """Recovery is the head's backoff, not one per row: the rows behind it
        were never attempted, so they owe no backoff of their own."""
        _wi, error, recovered = await _episode(db_session)
        error.attempts = 4
        error.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_watch_events(bus_client=client)

        assert result["published"] == 2

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(NoPermissionError("NOPERM no permissions to run 'xadd'"), id="noperm"),
            pytest.param(RedisConnectionError("refused"), id="connection"),
        ],
    )
    async def test_an_outage_is_transient_and_never_dead_letters(
        self, db_session, monkeypatch, exc
    ):
        """broker#93's denial is the expected one before the grant: an outage,
        not poison — the fact publishes unchanged once it clears."""
        wi = await make_watched_item(db_session)
        row = enqueue_watch_error(db_session, wi, now=NOW, reason="http_status", status_code=503)
        row.attempts = MAX_PUBLISH_ATTEMPTS + 5
        await db_session.commit()
        _wire(db_session, monkeypatch)

        result = await drain_pending_watch_events(bus_client=refusing_client(exc))

        assert result["failed"] == 1
        await db_session.refresh(row)
        assert row.dead_lettered_at is None

    async def test_a_persistent_non_transient_error_dead_letters_at_the_ceiling(
        self, db_session, monkeypatch
    ):
        wi = await make_watched_item(db_session)
        row = enqueue_watch_error(db_session, wi, now=NOW, reason="http_status", status_code=503)
        row.attempts = MAX_PUBLISH_ATTEMPTS - 1
        await db_session.commit()
        _wire(db_session, monkeypatch)

        result = await drain_pending_watch_events(
            bus_client=refusing_client(ResponseError("WRONGTYPE"))
        )

        assert result["dead_lettered"] == 1
        await db_session.refresh(row)
        assert row.dead_lettered_at is not None


class TestDeadLetter:
    async def test_a_refused_payload_dead_letters_and_the_queue_moves_on(
        self, db_session, monkeypatch
    ):
        """D2: a ``None`` reason is staged at the edge and refused here; the
        row stays for post-mortem and the fact behind it still publishes."""
        wi = await make_watched_item(db_session)
        bad = enqueue_watch_error(db_session, wi, now=NOW, reason=None, status_code=None)
        good = enqueue_watch_recovered(db_session, wi, now=NOW, error_since=None)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()

        result = await drain_pending_watch_events(bus_client=client)

        assert result == {"published": 1, "failed": 0, "dead_lettered": 1}
        [(key, _payload)] = await _entries(client)
        assert key == str(good.id)
        [left] = await _rows(db_session)
        assert left.id == bad.id
        assert left.dead_lettered_at is not None
        assert left.last_error.startswith("unbuildable_payload:")

    async def test_an_unknown_kind_dead_letters(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session)
        row = enqueue_watch_error(db_session, wi, now=NOW, reason="x", status_code=None)
        row.event_type = "never_heard_of_it"
        await db_session.commit()
        _wire(db_session, monkeypatch)

        result = await drain_pending_watch_events(bus_client=fakeredis.FakeAsyncRedis())

        assert result["dead_lettered"] == 1

    async def test_dead_lettered_rows_are_never_selected_again(self, db_session, monkeypatch):
        wi = await make_watched_item(db_session)
        row = enqueue_watch_error(db_session, wi, now=NOW, reason=None, status_code=None)
        await db_session.commit()
        _wire(db_session, monkeypatch)
        client = fakeredis.FakeAsyncRedis()
        await drain_pending_watch_events(bus_client=client)
        _wire(db_session, monkeypatch)

        result = await drain_pending_watch_events(bus_client=client)

        assert result == {"published": 0, "failed": 0, "dead_lettered": 0}
        await db_session.refresh(row)
        assert row.attempts == 1


class TestNoBus:
    async def test_idle_without_a_bus_is_silent(self, db_session, monkeypatch, caplog):
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(drain_mod, "get_shared_bus_client", lambda: None)

        result = await drain_pending_watch_events()

        assert result == {"published": 0, "failed": 0, "dead_lettered": 0}
        assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]

    async def test_queued_facts_without_a_bus_say_so(self, db_session, monkeypatch, caplog):
        await _episode(db_session)
        _wire(db_session, monkeypatch)
        monkeypatch.setattr(drain_mod, "get_shared_bus_client", lambda: None)

        result = await drain_pending_watch_events()

        assert result["skipped"] == "no_bus"
        assert len(await _rows(db_session)) == 2
        assert any(r.levelname == "ERROR" for r in caplog.records)
