"""Tests for the info.watch-status producer (#264).

Watcher publishes its scheduler state as a broadcast LWW level stream — the
return leg of ``info.registry``. These tests pin the producer-side rules the
consumer cannot check for us:

* every publish is built from **committed rows**, so ``applied_generation``
  can never travel before its reconcile commits (the premature-stamp hazard);
* ``applied_active`` is what the scheduler actually gates on — the conjunction
  of ``is_active``, un-archived, and un-suspended — never a bare echo of the
  announcement;
* ``applied_interval`` is the concrete resolved cadence **after** the throttle
  floor, so a cadence-only divergence (unparseable spec, floor, delegation) is
  visible to Archiver's drift detector even though ``applied_active`` never
  moves;
* ``applied_generation`` publishes ``0`` for a never-reconciled row — the
  sentinel collapses "never reconciled" and "reconciled at generation 0" (a
  never-mutated InfoItem announces at 0), which is benign under
  apply-iff-greater and self-correcting: the consumer replays the registry at
  every boot, and the first mutation bumps past 0;
* tombstones (``revoked=True``) ride ``revoked_info_items`` into every full
  set, and an unpublishable row is skipped with a warning, never allowed to
  fail the batch.
"""

from datetime import UTC, datetime

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import WatchStatusState
from ulid import ULID

from src.core.models.revoked_info_item import RevokedInfoItem
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.watch_status import (
    DEFAULT_WATCH_STATUS_STREAM_MAXLEN,
    WATCH_STATUS_STREAM_MAXLEN_ENV,
    build_status_events,
    publish_full_status_set,
    publish_status_events,
)

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
CHECKED = datetime(2026, 8, 14, 11, 0, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 8, 14, 10, 30, 0, tzinfo=UTC)


def _item(**overrides) -> WatchedItem:
    kwargs = {
        "archiver_info_item_id": ULID(),
        "name": "LCB rules",
        "effective_url": "https://lcb.wa.gov/rules",
        "archiver_info_source_id": str(ULID()),
        "applied_generation": 7,
        "health_status": WatchHealthStatus.OK,
        "announced_schedule_config": {"interval": "15m"},
        "last_checked_at": CHECKED,
        "last_observed_at": OBSERVED,
    }
    kwargs.update(overrides)
    return WatchedItem(**kwargs)


def _tombstone(generation: int = 3) -> RevokedInfoItem:
    return RevokedInfoItem(info_item_id=str(ULID()), generation=generation, revoked_at=NOW)


class TestLiveMapping:
    def test_reconciled_healthy_item_maps_completely(self):
        item = _item()
        events = build_status_events([item], [], now=NOW)
        assert len(events) == 1
        event = events[0]
        assert event.info_item_id == str(item.archiver_info_item_id)
        assert event.applied_generation == 7
        assert event.applied_active is True
        assert event.health == "ok"
        assert event.applied_interval == "15m"
        assert event.last_attempt_at == CHECKED
        assert event.last_observed_at == OBSERVED
        assert event.revoked is False
        assert event.occurred_at == NOW

    def test_never_reconciled_row_publishes_the_zero_sentinel(self):
        # 0 means "nothing newer than generation 0 applied" — a never-mutated
        # InfoItem announces at 0, so this collapses "never reconciled" with
        # "reconciled at 0"; benign either way under apply-iff-greater, and
        # any real mutation's generation (>= 1) supersedes it.
        events = build_status_events([_item(applied_generation=None)], [], now=NOW)
        assert events[0].applied_generation == 0

    def test_timestamps_may_be_absent(self):
        events = build_status_events(
            [_item(last_checked_at=None, last_observed_at=None)], [], now=NOW
        )
        assert events[0].last_attempt_at is None
        assert events[0].last_observed_at is None


class TestHealthMapping:
    def test_error_maps_to_error(self):
        events = build_status_events([_item(health_status=WatchHealthStatus.ERROR)], [], now=NOW)
        assert events[0].health == "error"

    def test_unknown_maps_to_unknown(self):
        events = build_status_events([_item(health_status=WatchHealthStatus.UNKNOWN)], [], now=NOW)
        assert events[0].health == "unknown"

    def test_probing_maps_to_unknown(self):
        # PROBING is producer mechanism (bus-mode URL-first create awaiting its
        # first fact, #241); the registry only needs "not yet verified".
        events = build_status_events([_item(health_status=WatchHealthStatus.PROBING)], [], now=NOW)
        assert events[0].health == "unknown"

    def test_null_health_maps_to_unknown(self):
        events = build_status_events([_item(health_status=None)], [], now=NOW)
        assert events[0].health == "unknown"


class TestAppliedActive:
    def test_paused_item_is_not_active(self):
        events = build_status_events([_item(is_active=False)], [], now=NOW)
        assert events[0].applied_active is False

    def test_archived_item_is_not_active(self):
        # The scheduler gates on archived_at IS NULL independently of
        # is_active; an active-but-archived row is not being fetched.
        events = build_status_events([_item(archived_at=NOW)], [], now=NOW)
        assert events[0].applied_active is False

    def test_suspended_domain_stops_the_item(self):
        events = build_status_events([_item(domain_suspended=True)], [], now=NOW)
        assert events[0].applied_active is False


class TestAppliedInterval:
    def test_announced_interval_is_reported_when_in_force(self):
        events = build_status_events(
            [_item(announced_schedule_config={"interval": "15m"})], [], now=NOW
        )
        assert events[0].applied_interval == "15m"

    def test_fallback_reports_the_concrete_local_cadence(self):
        # Delegation / unparseable both land announced_schedule_config = NULL;
        # the wire carries the interval actually in force, so Archiver's
        # drift detector sees applied != announced and next_due derives true.
        events = build_status_events(
            [
                _item(
                    announced_schedule_config=None,
                    default_schedule_config={"interval": "6h"},
                )
            ],
            [],
            now=NOW,
        )
        assert events[0].applied_interval == "6h"

    def test_system_default_is_still_a_concrete_interval(self):
        events = build_status_events([_item(announced_schedule_config=None)], [], now=NOW)
        assert events[0].applied_interval == "1d"

    def test_throttle_floor_overrides_a_faster_announced_interval(self):
        # The floor is the one divergence the announced tier cannot see:
        # max(resolved, floor) is what the scheduler runs, so it is what
        # publishes — deriving next_due from the announced 15m would render
        # the item permanently overdue.
        events = build_status_events(
            [
                _item(
                    announced_schedule_config={"interval": "15m"},
                    throttle_floor_interval="1d",
                )
            ],
            [],
            now=NOW,
        )
        assert events[0].applied_interval == "1d"

    def test_explicit_empty_config_reports_none(self):
        events = build_status_events([_item(announced_schedule_config={})], [], now=NOW)
        assert events[0].applied_interval is None


class TestTombstones:
    def test_tombstone_becomes_revoked_event(self):
        tomb = _tombstone(generation=9)
        events = build_status_events([], [tomb], now=NOW)
        assert len(events) == 1
        event = events[0]
        assert event.info_item_id == tomb.info_item_id
        assert event.revoked is True
        assert event.applied_generation == 9
        assert event.health is None
        assert event.applied_active is None

    def test_full_set_is_items_plus_tombstones(self):
        item = _item()
        tomb = _tombstone()
        events = build_status_events([item], [tomb], now=NOW)
        assert {(e.info_item_id, e.revoked) for e in events} == {
            (str(item.archiver_info_item_id), False),
            (tomb.info_item_id, True),
        }

    def test_unpublishable_row_is_skipped_not_fatal(self, caplog):
        # Generation is ge=0 on the wire; a corrupt row must not stop the rest
        # of the corpus's statuses from travelling.
        bad = RevokedInfoItem(info_item_id="bad", generation=-1, revoked_at=NOW)
        with caplog.at_level("WARNING"):
            events = build_status_events([_item()], [bad], now=NOW)
        assert len(events) == 1
        assert events[0].revoked is False
        skipped = [r for r in caplog.records if "skipping unpublishable" in r.getMessage()]
        assert len(skipped) == 1


class TestPublishStatusEvents:
    async def test_frames_land_decodable_on_the_status_stream(self):
        client = fakeredis.FakeAsyncRedis()
        events = build_status_events([_item()], [_tombstone()], now=NOW)
        published = await publish_status_events(client, events)
        assert published == 2

        entries = await client.xrange(streams.INFO_WATCH_STATUS)
        assert len(entries) == 2
        decoded = []
        for _message_id, fields in entries:
            frame = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            message = from_wire(frame, topic=streams.INFO_WATCH_STATUS)
            decoded.append(message.payload)
        assert all(isinstance(p, WatchStatusState) for p in decoded)
        by_revoked = {p.revoked: p for p in decoded}
        assert by_revoked[False].health == "ok"
        assert by_revoked[True].applied_generation == 3


@pytest.mark.integration
class TestPublishFullStatusSet:
    async def test_full_set_reads_items_and_tombstones(self, db_session):
        item = _item()
        db_session.add(item)
        db_session.add(_tombstone(generation=5))
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        published = await publish_full_status_set(db_session, client)
        assert published == 2

        entries = await client.xrange(streams.INFO_WATCH_STATUS)
        keys = set()
        for _message_id, fields in entries:
            frame = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            keys.add(from_wire(frame, topic=streams.INFO_WATCH_STATUS).payload.info_item_id)
        assert str(item.archiver_info_item_id) in keys
        assert len(keys) == 2

    async def test_the_stream_only_ever_reflects_committed_state(self, db_session):
        # The premature-stamp guard, by construction: the publisher reads rows,
        # so a reconcile that fails before commit cannot advance the published
        # generation — there is no in-flight value to leak.
        item = _item(applied_generation=4)
        db_session.add(item)
        await db_session.commit()

        item.applied_generation = 5
        await db_session.rollback()

        client = fakeredis.FakeAsyncRedis()
        await publish_full_status_set(db_session, client)
        entries = await client.xrange(streams.INFO_WATCH_STATUS)
        assert len(entries) == 1
        frame = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in entries[0][1].items()
        }
        payload = from_wire(frame, topic=streams.INFO_WATCH_STATUS).payload
        assert payload.applied_generation == 4


class TestBuilderHardening:
    def test_corrupt_interval_falls_back_to_none_not_a_dropped_frame(self, caplog):
        # CR-5: one corrupt cadence value must not silence the item's whole
        # status — the generation ack matters more than the interval.
        item = _item(announced_schedule_config={"interval": 123})
        with caplog.at_level("WARNING"):
            events = build_status_events([item], [], now=NOW)
        assert len(events) == 1
        assert events[0].applied_interval is None
        assert events[0].applied_generation == 7
        assert events[0].health == "ok"
        degraded = [
            r for r in caplog.records if "publishing without applied_interval" in r.getMessage()
        ]
        assert len(degraded) == 1


class TestStreamRetention:
    """CR-1: the module docstring promises retention rides the producer's
    maxlen — so every publish must actually carry one; unbounded is never an
    option (the full set republishes every few minutes forever)."""

    def test_resolver_defaults_and_overrides(self, monkeypatch):
        from src.core.bus import resolve_stream_maxlen

        monkeypatch.delenv(WATCH_STATUS_STREAM_MAXLEN_ENV, raising=False)
        assert resolve_stream_maxlen(WATCH_STATUS_STREAM_MAXLEN_ENV, 50_000) == 50_000
        monkeypatch.setenv(WATCH_STATUS_STREAM_MAXLEN_ENV, "1234")
        assert resolve_stream_maxlen(WATCH_STATUS_STREAM_MAXLEN_ENV, 50_000) == 1234

    def test_resolver_never_returns_unbounded(self, monkeypatch, caplog):
        from src.core.bus import resolve_stream_maxlen

        for bad in ("not-a-number", "0", "-5"):
            monkeypatch.setenv(WATCH_STATUS_STREAM_MAXLEN_ENV, bad)
            with caplog.at_level("WARNING"):
                assert resolve_stream_maxlen(WATCH_STATUS_STREAM_MAXLEN_ENV, 50_000) == 50_000

    async def test_every_publish_carries_maxlen(self, monkeypatch):
        captured = []

        class _CapturingPublisher:
            def __init__(self, client):
                pass

            async def execute(self, effect):
                captured.append(effect)

        import src.core.watch_status as ws_mod

        monkeypatch.setattr(ws_mod, "AsyncBusPublisher", _CapturingPublisher)
        events = build_status_events([_item()], [_tombstone()], now=NOW)
        await publish_status_events(object(), events)
        assert len(captured) == 2
        assert all(e.maxlen == DEFAULT_WATCH_STATUS_STREAM_MAXLEN for e in captured)


class TestBuilderHardeningWithFloor:
    def test_corrupt_interval_under_a_floor_publishes_the_floor(self):
        # CR-7: parse_interval raises TypeError (not ValueError) on a non-str
        # interval; before the resolution catch was broadened this killed the
        # whole batch. With it, the floor wins — also the truthful cadence.
        item = _item(
            announced_schedule_config={"interval": 123},
            throttle_floor_interval="1d",
        )
        events = build_status_events([item], [], now=NOW)
        assert len(events) == 1
        assert events[0].applied_interval == "1d"
        assert events[0].applied_generation == 7
