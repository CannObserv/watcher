"""Tests for the content.fetch-policy producer (#245).

Watcher is the policy half of the politeness split (replicator#12): it owns the
per-host numbers in ``Domain`` and publishes them as ``FetchPolicyState`` frames
on ``content.fetch-policy``. These tests pin the three rules the consumer side
cannot check for us:

* the published interval is ``Domain.min_interval`` — the operator floor
  (the limiter's ``current_interval`` backoff state is dropped — #272);
* tombstones (``revoked=True``) keep appearing in the full-set republish so a
  booting consumer can never replay a stale live value it cannot revoke
  (cannobserv#285 rule 2);
* a suspended domain — archived or deactivated — publishes ``revoked=True``
  rather than a live interval (#250);
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


def _domain(
    name: str,
    *,
    min_interval: float = 1.0,
    is_active: bool = True,
    archived_at: datetime | None = None,
) -> Domain:
    return Domain(
        name=name,
        min_interval=min_interval,
        is_active=is_active,
        archived_at=archived_at,
    )


def _tombstone(host: str) -> FetchPolicyTombstone:
    return FetchPolicyTombstone(host=host, revoked_at=NOW)


async def _decode_stream(client) -> list[FetchPolicyState]:
    """Every frame on content.fetch-policy, decoded back through the envelope."""
    payloads = []
    for _message_id, fields in await client.xrange(streams.CONTENT_FETCH_POLICY):
        frame = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in fields.items()
        }
        payloads.append(from_wire(frame, topic=streams.CONTENT_FETCH_POLICY).payload)
    return payloads


class TestBuildPolicyEvents:
    def test_domain_becomes_live_policy(self):
        events = build_policy_events([_domain("lcb.wa.gov", min_interval=2.5)], [], now=NOW)
        assert len(events) == 1
        event = events[0]
        assert event.host == "lcb.wa.gov"
        assert event.min_interval_seconds == 2.5
        assert event.revoked is False
        assert event.occurred_at == NOW

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


class TestSuspendedDomainsRevoke:
    """#250: suspended for any reason ⇒ no live policy published.

    ``revoked`` is cannobserv#285's tombstone — "no explicit policy for this
    host", *not* "no limit": the consumer falls back to its own conservative
    default, which rule 1 requires be at least as strict as anything the
    producer would publish. So revoking a host Watcher has stopped watching
    cannot open a politeness gap, while republishing a live ``min_interval``
    for it is the one way that host ends up *looser* than the fallback.

    Rule 2 ("keep republishing revoked hosts") is satisfied without bookkeeping:
    the ``Domain`` row survives archive and deactivation, so a suspended host
    keeps appearing in every full set — now as a tombstone.
    """

    def test_archived_domain_is_revoked(self):
        archived = _domain("archived.example", min_interval=2.5, archived_at=NOW)
        events = build_policy_events([archived], [], now=NOW)
        assert len(events) == 1
        assert events[0].host == "archived.example"
        assert events[0].revoked is True
        # Never a fake number: a consumer that ignores `revoked` and reaches for
        # the interval must get a None that fails loudly, not a stale value.
        assert events[0].min_interval_seconds is None

    def test_deactivated_domain_is_revoked(self):
        inactive = _domain("paused.example", min_interval=2.5, is_active=False)
        events = build_policy_events([inactive], [], now=NOW)
        assert len(events) == 1
        assert events[0].revoked is True
        assert events[0].min_interval_seconds is None

    def test_archived_and_deactivated_domain_is_revoked_once(self):
        both = _domain("both.example", is_active=False, archived_at=NOW)
        events = build_policy_events([both], [], now=NOW)
        assert [(e.host, e.revoked) for e in events] == [("both.example", True)]

    def test_restored_domain_publishes_live_policy_again(self):
        # Restore/reactivate needs no operator action and no bookkeeping: the
        # next full set reads the cleared columns and emits live again.
        restored = _domain("restored.example", min_interval=4.0, archived_at=None, is_active=True)
        events = build_policy_events([restored], [], now=NOW)
        assert events[0].revoked is False
        assert events[0].min_interval_seconds == 4.0

    def test_suspended_domain_still_appears_in_the_full_set(self):
        # cannobserv#285 rule 2 — dropping the host from the republish would let
        # broker trimming age the tombstone out from under a booting consumer.
        events = build_policy_events(
            [
                _domain("live.example", min_interval=1.5),
                _domain("archived.example", archived_at=NOW),
                _domain("paused.example", is_active=False),
            ],
            [_tombstone("gone.example")],
            now=NOW,
        )
        assert {(e.host, e.revoked) for e in events} == {
            ("live.example", False),
            ("archived.example", True),
            ("paused.example", True),
            ("gone.example", True),
        }

    def test_archived_host_with_a_tombstone_row_agrees(self):
        # Domain rows and tombstone rows are normally disjoint (a tombstone is
        # written on delete and cleared on re-create), but if both exist for one
        # host both frames now say revoked — so LWW lands on the same state
        # whichever arrives last, instead of depending on emit order.
        events = build_policy_events(
            [_domain("both.example", archived_at=NOW)], [_tombstone("both.example")], now=NOW
        )
        assert [e.host for e in events] == ["both.example", "both.example"]
        assert all(e.revoked is True for e in events)

    def test_suspended_host_the_model_rejects_is_still_skipped(self, caplog):
        with caplog.at_level("WARNING"):
            events = build_policy_events(
                [_domain("bad.example:8080", archived_at=NOW), _domain("good.example")],
                [],
                now=NOW,
            )
        assert [e.host for e in events] == ["good.example"]
        assert any("skipping unpublishable" in r.getMessage() for r in caplog.records)


class TestPublishPolicyEvents:
    async def test_frames_land_decodable_on_the_policy_stream(self):
        client = fakeredis.FakeAsyncRedis()
        events = build_policy_events(
            [_domain("lcb.wa.gov", min_interval=2.0)], [_tombstone("gone.example")], now=NOW
        )
        published = await publish_policy_events(client, events)
        assert published == 2

        decoded = await _decode_stream(client)
        assert len(decoded) == 2
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
        db_session.add(Domain(name="live.example", min_interval=3.0))
        await record_tombstone(db_session, "gone.example", now=NOW)
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        published = await publish_full_policy_set(db_session, client)
        assert published == 2

        hosts = {p.host for p in await _decode_stream(client)}
        assert hosts == {"live.example", "gone.example"}

    async def test_full_set_revokes_archived_and_inactive_domains(self, db_session):
        # #250 end to end: the query still selects every Domain (rule 2), but a
        # suspended one travels as a tombstone rather than a live interval.
        db_session.add(Domain(name="live.example", min_interval=3.0))
        db_session.add(Domain(name="archived.example", min_interval=3.0, archived_at=NOW))
        db_session.add(Domain(name="paused.example", min_interval=3.0, is_active=False))
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        published = await publish_full_policy_set(db_session, client)
        assert published == 3

        by_host = {p.host: p for p in await _decode_stream(client)}
        assert by_host["live.example"].revoked is False
        assert by_host["live.example"].min_interval_seconds == 3.0
        assert by_host["archived.example"].revoked is True
        assert by_host["archived.example"].min_interval_seconds is None
        assert by_host["paused.example"].revoked is True

    async def test_full_set_publishes_live_again_after_restore(self, db_session):
        domain = Domain(name="restored.example", min_interval=3.0, archived_at=NOW, is_active=False)
        db_session.add(domain)
        await db_session.flush()

        domain.archived_at = None
        domain.is_active = True
        await db_session.flush()

        client = fakeredis.FakeAsyncRedis()
        await publish_full_policy_set(db_session, client)
        payloads = await _decode_stream(client)
        assert len(payloads) == 1
        assert payloads[0].revoked is False
        assert payloads[0].min_interval_seconds == 3.0


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
