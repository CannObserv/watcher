"""Tests for the content.fetch-policy producer (#245).

Watcher is the policy half of the politeness split (replicator#12): it owns the
per-host numbers in ``Domain`` and publishes them as ``FetchPolicyState`` frames
on ``content.fetch-policy``. These tests pin the three rules the consumer side
cannot check for us:

* the published interval is ``Domain.min_interval`` — the operator floor — never
  ``current_interval`` (a backoff artifact that freezes at the Phase-4 cutover);
* tombstones (``revoked=True``) keep appearing in the full-set republish so a
  booting consumer can never replay a stale live value it cannot revoke
  (cannobserv#285 rule 2);
* a host the model rejects is skipped with a warning, never allowed to fail the
  batch — one bad row must not stop the other 99 policies from publishing.
"""

from datetime import UTC, datetime

import fakeredis
import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.models.changes import FetchPolicyState

from src.core.fetch_policy import (
    build_policy_events,
    clear_tombstone,
    publish_full_policy_set,
    publish_policy_events,
    record_tombstone,
)
from src.core.models.domain import Domain
from src.core.models.fetch_policy_tombstone import FetchPolicyTombstone

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def _domain(name: str, *, min_interval: float = 1.0, current_interval: float = 1.0) -> Domain:
    return Domain(name=name, min_interval=min_interval, current_interval=current_interval)


def _tombstone(host: str) -> FetchPolicyTombstone:
    return FetchPolicyTombstone(host=host, revoked_at=NOW)


class TestBuildPolicyEvents:
    def test_domain_becomes_live_policy(self):
        events = build_policy_events([_domain("lcb.wa.gov", min_interval=2.5)], [], now=NOW)
        assert len(events) == 1
        event = events[0]
        assert event.host == "lcb.wa.gov"
        assert event.min_interval_seconds == 2.5
        assert event.revoked is False
        assert event.occurred_at == NOW

    def test_publishes_min_interval_never_current_interval(self):
        # current_interval is 429-backoff state; it freezes at the Phase-4
        # cutover (no non-terminal fetch_failed — replicator#9 §3), so
        # publishing it would fossilize a backoff artifact into policy.
        events = build_policy_events(
            [_domain("lcb.wa.gov", min_interval=1.0, current_interval=60.0)], [], now=NOW
        )
        assert events[0].min_interval_seconds == 1.0

    def test_tombstone_becomes_revoked_event(self):
        events = build_policy_events([], [_tombstone("gone.example")], now=NOW)
        assert len(events) == 1
        event = events[0]
        assert event.host == "gone.example"
        assert event.revoked is True
        assert event.min_interval_seconds is None

    def test_invalid_host_is_skipped_not_fatal(self, caplog):
        # A host FetchPolicyState rejects (non-ASCII, embedded port, …) must
        # not fail the batch: the other domains' policies still publish.
        domains = [
            _domain("good.example"),
            _domain("bad.example:8080"),
            _domain("überbad.example"),
        ]
        with caplog.at_level("WARNING"):
            events = build_policy_events(domains, [], now=NOW)
        assert [e.host for e in events] == ["good.example"]
        skipped = [r for r in caplog.records if "skipping unpublishable" in r.getMessage()]
        assert len(skipped) == 2

    def test_full_set_is_domains_plus_tombstones(self):
        events = build_policy_events(
            [_domain("a.example"), _domain("b.example")], [_tombstone("gone.example")], now=NOW
        )
        assert {(e.host, e.revoked) for e in events} == {
            ("a.example", False),
            ("b.example", False),
            ("gone.example", True),
        }


class TestPublishPolicyEvents:
    async def test_frames_land_decodable_on_the_policy_stream(self):
        client = fakeredis.FakeAsyncRedis()
        events = build_policy_events(
            [_domain("lcb.wa.gov", min_interval=2.0)], [_tombstone("gone.example")], now=NOW
        )
        published = await publish_policy_events(client, events)
        assert published == 2

        entries = await client.xrange(streams.CONTENT_FETCH_POLICY)
        assert len(entries) == 2
        decoded = []
        for message_id, fields in entries:
            frame = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            message = from_wire(frame, topic=streams.CONTENT_FETCH_POLICY)
            decoded.append(message.payload)
        assert all(isinstance(p, FetchPolicyState) for p in decoded)
        by_host = {p.host: p for p in decoded}
        assert by_host["lcb.wa.gov"].min_interval_seconds == 2.0
        assert by_host["gone.example"].revoked is True


@pytest.mark.integration
class TestTombstoneRows:
    async def test_record_then_clear_roundtrip(self, db_session):
        await record_tombstone(db_session, "gone.example", now=NOW)
        await db_session.flush()
        row = await db_session.get(FetchPolicyTombstone, "gone.example")
        assert row is not None
        assert row.revoked_at == NOW

        await clear_tombstone(db_session, "gone.example")
        await db_session.flush()
        assert await db_session.get(FetchPolicyTombstone, "gone.example") is None

    async def test_record_is_idempotent(self, db_session):
        await record_tombstone(db_session, "gone.example", now=NOW)
        await db_session.flush()
        later = datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC)
        await record_tombstone(db_session, "gone.example", now=later)
        await db_session.flush()
        row = await db_session.get(FetchPolicyTombstone, "gone.example")
        assert row.revoked_at == later

    async def test_clear_missing_is_a_noop(self, db_session):
        await clear_tombstone(db_session, "never.recorded.example")
        await db_session.flush()

    async def test_full_set_reads_domains_and_tombstones(self, db_session):
        db_session.add(Domain(name="live.example", min_interval=3.0, current_interval=3.0))
        await record_tombstone(db_session, "gone.example", now=NOW)
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        published = await publish_full_policy_set(db_session, client)
        assert published == 2

        entries = await client.xrange(streams.CONTENT_FETCH_POLICY)
        hosts = set()
        for _message_id, fields in entries:
            frame = {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in fields.items()
            }
            hosts.add(from_wire(frame, topic=streams.CONTENT_FETCH_POLICY).payload.host)
        assert hosts == {"live.example", "gone.example"}


class TestPolicyStreamRetention:
    """watcher#264 CR-3: the fetch-policy full set republishes every 5 minutes
    and had accumulated ~7k untrimmed entries in prod — every publish must
    carry a maxlen."""

    async def test_every_publish_carries_maxlen(self, monkeypatch):
        captured = []

        class _CapturingPublisher:
            def __init__(self, client):
                pass

            async def execute(self, effect):
                captured.append(effect)

        import src.core.fetch_policy as fp_mod

        monkeypatch.setattr(fp_mod, "AsyncBusPublisher", _CapturingPublisher)
        events = build_policy_events([_domain("lcb.wa.gov")], [_tombstone("gone.example")], now=NOW)
        await publish_policy_events(object(), events)
        assert len(captured) == 2
        assert all(e.maxlen == fp_mod.DEFAULT_FETCH_POLICY_STREAM_MAXLEN for e in captured)
