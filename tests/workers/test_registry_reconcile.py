"""Tests for the info.registry reconcile (#254).

The contract rules this file pins, each of which has a way to get it wrong that
is silent rather than loud:

* **Branch on ``revoked`` first.** Collapsing paused into revoked loses the pause
  on the next reconcile.
* **Apply iff ``generation >`` stored**, never ``!=`` — the producer's outbox
  drain reorders under retry, so a stale announcement genuinely lands after a
  fresh one on a last-write-wins stream.
* **A revoked key keeps its generation** in ``revoked_info_items``. Deleting the
  WatchedItem would otherwise disarm the guard above for exactly the keys it
  matters most for.
* **``active is None`` is an abstention**, not a default. Treating it as ``True``
  un-pauses every item an operator paused, which is what the rollout window looks
  like before archiver#150's import populates the column.
* **An absent ``interval`` and an unparseable ``watch_spec`` both mean "apply your
  own default"** — and neither may stop scheduling.
* **Watcher-local columns survive.** The registry has no opinion on health,
  timings, suspension, or notification config.
"""

from datetime import UTC, datetime, timedelta

import pytest
from co_core.pure.adapters.bus import streams
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.models.changes import RegistryAnnouncementEmit
from sqlalchemy import select
from ulid import ULID

from src.core.models.revoked_info_item import RevokedInfoItem
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.scheduling.resolution import resolved_schedule_config
from src.workers.registry_reconcile import reconcile_announcement
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)

LIVE_SPECS = [{"selector": "main#content"}]


def _announcement(
    info_item_id,
    *,
    generation=1,
    revoked=False,
    url="https://lcb.wa.gov/notices",
    info_source_id=None,
    source_specs=None,
    active=True,
    watch_spec=None,
    occurred_at=NOW,
):
    """A decoded ``info.registry`` message.

    Built through ``to_wire``/``from_wire`` rather than by constructing the state
    model directly, so every test exercises the real decode path — including the
    producer-side validator that makes ``watch_spec`` required when live.
    """
    if revoked:
        event = RegistryAnnouncementEmit(
            occurred_at=occurred_at,
            info_item_id=str(info_item_id),
            generation=generation,
            revoked=True,
        )
    else:
        event = RegistryAnnouncementEmit(
            occurred_at=occurred_at,
            info_item_id=str(info_item_id),
            generation=generation,
            info_source_id=info_source_id or str(ULID()),
            url=url,
            source_specs=source_specs if source_specs is not None else LIVE_SPECS,
            active=active,
            watch_spec=watch_spec if watch_spec is not None else {"schema_version": 1},
        )
    return from_wire(to_wire(event), topic=streams.INFO_REGISTRY, message_id="1-1").payload


async def _get(session, info_item_id) -> WatchedItem | None:
    return (
        (
            await session.execute(
                select(WatchedItem).where(
                    WatchedItem.archiver_info_item_id == ULID.from_str(str(info_item_id))
                )
            )
        )
        .scalars()
        .one_or_none()
    )


class TestColdStartCreate:
    """A fresh Watcher, or one whose stream position is lost, must converge from
    the snapshot alone — that is what the snapshot is for."""

    async def test_creates_a_watched_item_from_an_announcement_alone(self, db_session):
        info_item_id = ULID()
        source_id = str(ULID())

        outcome = await reconcile_announcement(
            db_session,
            _announcement(
                info_item_id,
                info_source_id=source_id,
                url="https://lcb.wa.gov/notices",
                watch_spec={"schema_version": 1, "interval": "6h"},
            ),
        )

        assert outcome == "created"
        wi = await _get(db_session, info_item_id)
        assert wi is not None
        assert wi.effective_url == "https://lcb.wa.gov/notices"
        assert wi.archiver_info_source_id == source_id
        assert wi.source_specs == LIVE_SPECS
        assert wi.announced_schedule_config == {"interval": "6h"}
        assert wi.is_active is True
        assert wi.applied_generation == 1

    async def test_create_derives_the_domain_and_its_denormalized_state(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, url="https://lcb.wa.gov/notices")
        )
        wi = await _get(db_session, info_item_id)
        assert wi.domain_name == "lcb.wa.gov"
        assert wi.domain_suspended is False

    async def test_create_derives_a_name_because_the_announcement_carries_none(self, db_session):
        """`RegistryAnnouncementState` has no `name` field. The column is NOT NULL,
        so the reconcile derives a legible placeholder from the URL rather than
        inventing registry data."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, url="https://lcb.wa.gov/notices/board")
        )
        wi = await _get(db_session, info_item_id)
        assert wi.name == "lcb.wa.gov/notices/board"

    async def test_health_starts_unknown_not_ok(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id))
        wi = await _get(db_session, info_item_id)
        assert wi.health_status == WatchHealthStatus.UNKNOWN


class TestOrdering:
    """`generation` is the ordering token; the comparison is `>`, never `!=`."""

    async def test_a_newer_generation_applies(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        outcome = await reconcile_announcement(
            db_session,
            _announcement(info_item_id, generation=2, url="https://lcb.wa.gov/updated"),
        )
        assert outcome == "updated"
        wi = await _get(db_session, info_item_id)
        assert wi.effective_url == "https://lcb.wa.gov/updated"
        assert wi.applied_generation == 2

    async def test_an_older_generation_is_ignored_not_applied(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=7, url="https://lcb.wa.gov/fresh")
        )
        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=5, url="https://lcb.wa.gov/stale")
        )
        assert outcome == "stale"
        wi = await _get(db_session, info_item_id)
        assert wi.effective_url == "https://lcb.wa.gov/fresh"
        assert wi.applied_generation == 7

    async def test_the_same_generation_is_ignored(self, db_session):
        """Redelivery on replay: `>` not `>=`, so re-reading the stream at boot is
        a no-op rather than a rewrite."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=3, url="https://lcb.wa.gov/a")
        )
        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=3, url="https://lcb.wa.gov/b")
        )
        assert outcome == "stale"
        wi = await _get(db_session, info_item_id)
        assert wi.effective_url == "https://lcb.wa.gov/a"

    async def test_generation_zero_applies_on_a_key_never_seen(self, db_session):
        """NULL `applied_generation` means "never applied"; every generation
        exceeds it, and the contract permits 0 while rejecting negatives."""
        info_item_id = ULID()
        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=0)
        )
        assert outcome == "created"
        assert (await _get(db_session, info_item_id)).applied_generation == 0


class TestRevocation:
    async def test_revoked_deletes_the_watched_item(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))

        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, revoked=True)
        )

        assert outcome == "revoked"
        assert await _get(db_session, info_item_id) is None

    async def test_revocation_records_the_generation_that_did_it(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, revoked=True)
        )

        tomb = await db_session.get(RevokedInfoItem, str(info_item_id))
        assert tomb is not None
        assert tomb.generation == 2

    async def test_a_stale_live_announcement_does_not_resurrect_a_revoked_item(self, db_session):
        """The reordering `generation` exists to defeat, in its sharpest form: the
        row that would have held the guard has been deleted."""
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=5))
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=6, revoked=True)
        )

        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=5)
        )

        assert outcome == "stale"
        assert await _get(db_session, info_item_id) is None

    async def test_revoking_a_key_never_seen_still_records_the_tombstone(self, db_session):
        """Otherwise the tombstone-before-live ordering has nothing to compare
        against either."""
        info_item_id = ULID()
        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=4, revoked=True)
        )
        assert outcome == "revoked"
        assert (await db_session.get(RevokedInfoItem, str(info_item_id))).generation == 4

    async def test_a_newer_live_announcement_un_revokes(self, db_session):
        """`revoked` is a level, not an event: the registry may bring a key back."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, revoked=True)
        )

        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=3, url="https://lcb.wa.gov/back")
        )

        assert outcome == "created"
        wi = await _get(db_session, info_item_id)
        assert wi.effective_url == "https://lcb.wa.gov/back"
        assert await db_session.get(RevokedInfoItem, str(info_item_id)) is None

    async def test_a_stale_revocation_is_ignored(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=9))
        outcome = await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=4, revoked=True)
        )
        assert outcome == "stale"
        assert await _get(db_session, info_item_id) is not None


class TestActiveStates:
    """Four cases, and the fourth is not a state."""

    async def test_active_true_schedules(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, active=True))
        assert (await _get(db_session, info_item_id)).is_active is True

    async def test_active_false_keeps_the_row_and_stops_scheduling(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, active=False)
        )

        wi = await _get(db_session, info_item_id)
        assert wi is not None  # NOT deleted — paused is not revoked
        assert wi.is_active is False

    async def test_active_none_leaves_a_paused_item_paused(self, db_session):
        """The abstention. Treating `None` as `True` un-pauses every item an
        operator paused, which is exactly the rollout window before
        archiver#150's import populates the column."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=1, active=False)
        )

        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, active=None)
        )

        assert (await _get(db_session, info_item_id)).is_active is False

    async def test_active_none_leaves_an_active_item_active(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=1, active=True)
        )
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, active=None)
        )
        assert (await _get(db_session, info_item_id)).is_active is True

    async def test_active_none_on_create_falls_to_the_model_default(self, db_session):
        """There is nothing to abstain *from* on a create; the row has to start
        somewhere, and the WatchedItem default is active."""
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, active=None))
        assert (await _get(db_session, info_item_id)).is_active is True

    async def test_the_local_pause_is_not_sticky(self, db_session):
        """archiver#150's break-glass ruling: apply `active` unconditionally. Item
        level pause lives in exactly one place, and it is not here."""
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.is_active = False  # an operator pauses locally
        await db_session.commit()

        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, active=True)
        )

        assert (await _get(db_session, info_item_id)).is_active is True


class TestArchived:
    """An `active: true` announcement against an archived row no-ops and reports —
    it does not resurrect. Archiver's dashboard already handles Watcher 409ing
    pause/resume on an archived item, so the behaviour exists; this makes it
    deliberate on the reconcile side."""

    async def test_an_active_announcement_does_not_clear_archived_at(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.archived_at = NOW - timedelta(days=3)
        await db_session.commit()

        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, active=True)
        )

        wi = await _get(db_session, info_item_id)
        assert wi.archived_at == NOW - timedelta(days=3)

    async def test_an_archived_item_still_reconciles_its_specs(self, db_session):
        """No-op means *not scheduled*, not *not reconciled* — the row must be
        correct if it is ever restored."""
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.archived_at = NOW
        await db_session.commit()

        await reconcile_announcement(
            db_session,
            _announcement(info_item_id, generation=2, source_specs=[{"selector": "#new"}]),
        )

        assert (await _get(db_session, info_item_id)).source_specs == [{"selector": "#new"}]


class TestCadence:
    async def test_a_parseable_interval_lands_in_the_announced_tier(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(
            db_session,
            _announcement(info_item_id, watch_spec={"schema_version": 1, "interval": "15m"}),
        )
        wi = await _get(db_session, info_item_id)
        assert wi.announced_schedule_config == {"interval": "15m"}
        assert resolved_schedule_config(wi) == {"interval": "15m"}

    async def test_no_interval_delegates_to_the_local_default(self, db_session):
        """cannobserv#324: `{"schema_version": 1}` with no `interval` is how the
        producer says *apply your own default*. For this repo that is the
        per-domain tier, which the tightening deliberately kept live."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, watch_spec={"schema_version": 1})
        )
        wi = await _get(db_session, info_item_id)
        assert wi.announced_schedule_config is None
        wi.domain_default_schedule_config = {"interval": "7d"}
        assert resolved_schedule_config(wi) == {"interval": "7d"}

    async def test_an_unparseable_interval_does_not_stop_scheduling(self, db_session):
        """The failure mode this whole channel exists to remove. co-core does not
        validate the document's contents, deliberately — so the tolerance has to
        live here."""
        info_item_id = ULID()
        outcome = await reconcile_announcement(
            db_session,
            _announcement(
                info_item_id, watch_spec={"schema_version": 1, "interval": "every other tuesday"}
            ),
        )

        assert outcome == "created"
        wi = await _get(db_session, info_item_id)
        assert wi.is_active is True  # still scheduled
        assert wi.announced_schedule_config is None  # fell back to the local chain
        assert resolved_schedule_config(wi) == {"interval": "1d"}  # system default

    async def test_a_non_dict_watch_spec_does_not_raise(self, db_session):
        """`watch_spec` is an untyped document by contract; a consumer that cannot
        parse it must keep its own cadence rather than die."""
        info_item_id = ULID()
        payload = _announcement(info_item_id)
        object.__setattr__(payload, "watch_spec", {"schema_version": 1, "interval": ["6h"]})

        outcome = await reconcile_announcement(db_session, payload)

        assert outcome == "created"
        assert (await _get(db_session, info_item_id)).announced_schedule_config is None

    async def test_an_interval_that_disappears_clears_the_announced_tier(self, db_session):
        """Level-triggered: the registry withdrawing its cadence must fall back,
        not leave the last announced value pinned forever."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session,
            _announcement(
                info_item_id, generation=1, watch_spec={"schema_version": 1, "interval": "15m"}
            ),
        )
        await reconcile_announcement(
            db_session,
            _announcement(info_item_id, generation=2, watch_spec={"schema_version": 1}),
        )
        assert (await _get(db_session, info_item_id)).announced_schedule_config is None


class TestLocalColumnsSurvive:
    """The registry has no opinion on any of these; reconciliation must leave
    them intact. Named explicitly because "we did not write it" is not the same
    guarantee as "a test fails if someone does"."""

    async def test_health_timings_and_media_type_survive(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.health_status = WatchHealthStatus.ERROR
        wi.last_checked_at = NOW - timedelta(hours=2)
        wi.last_changed_at = NOW - timedelta(days=1)
        wi.last_reviewed_at = NOW - timedelta(days=5)
        wi.content_media_type = "application/pdf"
        wi.default_tags = ["lcb", "notices"]
        wi.description = "Operator's note"
        await db_session.commit()

        await reconcile_announcement(db_session, _announcement(info_item_id, generation=2))

        wi = await _get(db_session, info_item_id)
        assert wi.health_status == WatchHealthStatus.ERROR
        assert wi.last_checked_at == NOW - timedelta(hours=2)
        assert wi.last_changed_at == NOW - timedelta(days=1)
        assert wi.last_reviewed_at == NOW - timedelta(days=5)
        assert wi.content_media_type == "application/pdf"
        assert wi.default_tags == ["lcb", "notices"]
        assert wi.description == "Operator's note"

    async def test_domain_suspended_survives(self, db_session):
        """Host-level mechanism, on Watcher's side of the epic's role table. An
        announcement that re-derived it would un-suspend a host mid-incident."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=1, url="https://lcb.wa.gov/notices")
        )
        wi = await _get(db_session, info_item_id)
        wi.domain_suspended = True
        await db_session.commit()

        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, url="https://lcb.wa.gov/notices")
        )

        assert (await _get(db_session, info_item_id)).domain_suspended is True

    async def test_the_throttle_floor_survives(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.throttle_floor_interval = "1d"
        await db_session.commit()

        await reconcile_announcement(
            db_session,
            _announcement(
                info_item_id, generation=2, watch_spec={"schema_version": 1, "interval": "15m"}
            ),
        )

        wi = await _get(db_session, info_item_id)
        assert wi.throttle_floor_interval == "1d"
        assert resolved_schedule_config(wi) == {"interval": "1d"}

    async def test_the_local_cadence_tier_survives(self, db_session):
        """`default_schedule_config` is outranked by the announced tier, but it is
        not *overwritten* — archiver#150 reads it out of Watcher before the SDK
        is deleted, and it is the fallback the moment an announcement withdraws
        its interval."""
        info_item_id = ULID()
        await reconcile_announcement(db_session, _announcement(info_item_id, generation=1))
        wi = await _get(db_session, info_item_id)
        wi.default_schedule_config = {"interval": "6h"}
        await db_session.commit()

        await reconcile_announcement(
            db_session,
            _announcement(
                info_item_id, generation=2, watch_spec={"schema_version": 1, "interval": "15m"}
            ),
        )

        assert (await _get(db_session, info_item_id)).default_schedule_config == {"interval": "6h"}


class TestDomainMovement:
    async def test_a_host_change_re_derives_the_domain(self, db_session):
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=1, url="https://lcb.wa.gov/notices")
        )
        await reconcile_announcement(
            db_session,
            _announcement(info_item_id, generation=2, url="https://www.lcb.wa.gov/notices"),
        )
        wi = await _get(db_session, info_item_id)
        assert wi.domain_name == "www.lcb.wa.gov"

    async def test_a_same_host_url_change_leaves_suspension_alone(self, db_session):
        """Re-deriving on every announcement would clear a suspension the operator
        set — the host has not moved, so there is nothing to re-derive."""
        info_item_id = ULID()
        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=1, url="https://lcb.wa.gov/a")
        )
        wi = await _get(db_session, info_item_id)
        wi.domain_suspended = True
        await db_session.commit()

        await reconcile_announcement(
            db_session, _announcement(info_item_id, generation=2, url="https://lcb.wa.gov/b")
        )

        wi = await _get(db_session, info_item_id)
        assert wi.effective_url == "https://lcb.wa.gov/b"
        assert wi.domain_suspended is True


class TestMalformedIdentity:
    async def test_a_non_ulid_info_item_id_is_dropped_not_raised(self, db_session, caplog):
        """No DLQ on this stream: a message that cannot be applied is logged and
        dropped, and the next snapshot supersedes it. Raising would kill the
        consumer task instead."""
        payload = _announcement(ULID())
        object.__setattr__(payload, "info_item_id", "not-a-ulid")

        outcome = await reconcile_announcement(db_session, payload)

        assert outcome == "invalid"
        assert any("info_item_id" in r.message for r in caplog.records)


class TestPreexistingRow:
    """The rollout case: WatchedItems created through the POST route before the
    producer went live must be adopted by their first announcement, not
    duplicated — the unique index on the InfoItem link would reject the second."""

    async def test_an_existing_row_is_adopted_not_duplicated(self, db_session):
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        await db_session.commit()
        assert wi.applied_generation is None

        outcome = await reconcile_announcement(
            db_session,
            _announcement(wi.archiver_info_item_id, generation=1, url="https://lcb.wa.gov/renamed"),
        )

        assert outcome == "updated"
        refreshed = await _get(db_session, wi.archiver_info_item_id)
        assert refreshed.id == wi.id
        assert refreshed.effective_url == "https://lcb.wa.gov/renamed"

    async def test_adoption_keeps_the_operator_supplied_name(self, db_session):
        """The announcement carries no `name`, so it has no opinion to apply."""
        wi = await make_watched_item(db_session, name="WSLCB Board Notices")
        await db_session.commit()

        await reconcile_announcement(
            db_session, _announcement(wi.archiver_info_item_id, generation=1)
        )

        assert (await _get(db_session, wi.archiver_info_item_id)).name == "WSLCB Board Notices"


class TestConsumerLoop:
    """Boot behaviour, which is where a config/state consumer fails silently.

    A worker that reads from ``$`` sees nothing and is indistinguishable from a
    worker whose registry is genuinely empty — so the replay is the thing worth
    testing, not the steady-state delta path.
    """

    @staticmethod
    def _ctx(db_session):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _factory():
            yield db_session

        return _factory

    @staticmethod
    async def _publish(client, payload_event):
        from co_core.effects.bus import BusPublish
        from co_core_aio.bus import AsyncBusPublisher

        await AsyncBusPublisher(client).execute(
            BusPublish(streams.INFO_REGISTRY, to_wire(payload_event))
        )

    @staticmethod
    def _emit(info_item_id, **over):
        return RegistryAnnouncementEmit(
            occurred_at=over.pop("occurred_at", NOW),
            info_item_id=str(info_item_id),
            generation=over.pop("generation", 1),
            info_source_id=over.pop("info_source_id", str(ULID())),
            url=over.pop("url", "https://lcb.wa.gov/notices"),
            source_specs=over.pop("source_specs", LIVE_SPECS),
            active=over.pop("active", True),
            watch_spec=over.pop("watch_spec", {"schema_version": 1}),
            **over,
        )

    async def _run_until_quiet(self, client, db_session, monkeypatch, *, expect):
        """Run the consumer until ``expect`` announcements have been reconciled.

        Waits on a spy rather than by polling the database: the consumer holds
        the one session, and a concurrent query on it raises rather than merely
        racing.
        """
        import asyncio

        import src.workers.registry_reconcile as rr
        from src.workers.registry_reconcile import run_registry_consumer

        seen: list[str] = []
        real = rr.reconcile_announcement

        async def _spy(session, payload):
            outcome = await real(session, payload)
            seen.append(outcome)
            return outcome

        monkeypatch.setattr(rr, "reconcile_announcement", _spy)

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_registry_consumer(
                client, self._ctx(db_session), stop=stop, block_ms=10, error_backoff_seconds=0.01
            )
        )

        async def _until():
            while len(seen) < expect:
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(_until(), timeout=5)
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return seen

    async def test_cold_start_replays_from_the_beginning_of_the_stream(
        self, db_session, monkeypatch
    ):
        """Published BEFORE the consumer exists — a `$` read would see none of it."""
        import fakeredis

        client = fakeredis.FakeAsyncRedis()
        first, second = ULID(), ULID()
        await self._publish(client, self._emit(first, url="https://lcb.wa.gov/a"))
        await self._publish(client, self._emit(second, url="https://lcb.wa.gov/b"))

        await self._run_until_quiet(client, db_session, monkeypatch, expect=2)

        assert await _get(db_session, first) is not None
        assert await _get(db_session, second) is not None

    async def test_cold_start_converges_against_a_trimmed_stream(self, db_session, monkeypatch):
        """Retention here is a *consumer contract* carried by the producer's
        `maxlen`, not an operator `XTRIM` — so the boot path has to be correct
        against a stream that has already lost its oldest entries, not just an
        empty one. What survives trimming is the latest state per key, which is
        exactly what a last-write-wins consumer needs."""
        import fakeredis

        client = fakeredis.FakeAsyncRedis()
        item = ULID()
        await self._publish(client, self._emit(item, generation=1, url="https://lcb.wa.gov/old"))
        await self._publish(client, self._emit(item, generation=2, url="https://lcb.wa.gov/new"))
        # Trim to the newest entry only — the generation-1 announcement is gone.
        await client.xtrim(streams.INFO_REGISTRY, maxlen=1, approximate=False)

        await self._run_until_quiet(client, db_session, monkeypatch, expect=1)

        wi = await _get(db_session, item)
        assert wi.effective_url == "https://lcb.wa.gov/new"
        assert wi.applied_generation == 2

    async def test_a_poison_frame_is_seeked_past_not_re_read_forever(self, db_session, monkeypatch):
        """No group means no ack: the cursor must be advanced explicitly or the
        next read raises on the same frame forever."""
        import fakeredis

        client = fakeredis.FakeAsyncRedis()
        await client.xadd(streams.INFO_REGISTRY, {b"garbage": b"not-an-envelope"})
        good = ULID()
        await self._publish(client, self._emit(good))

        await self._run_until_quiet(client, db_session, monkeypatch, expect=1)

        assert await _get(db_session, good) is not None

    async def test_replay_is_idempotent_across_a_restart(self, db_session, monkeypatch):
        """The generation guard is what makes replaying the whole stream at every
        boot cheap rather than destructive."""
        import fakeredis

        client = fakeredis.FakeAsyncRedis()
        item = ULID()
        await self._publish(client, self._emit(item, generation=4))

        await self._run_until_quiet(client, db_session, monkeypatch, expect=1)
        wi = await _get(db_session, item)
        wi.health_status = WatchHealthStatus.OK
        wi.last_checked_at = NOW
        await db_session.commit()

        # Second boot: same stream, same cursor start.
        await self._run_until_quiet(client, db_session, monkeypatch, expect=1)

        wi = await _get(db_session, item)
        assert wi.applied_generation == 4
        assert wi.health_status == WatchHealthStatus.OK  # replay changed nothing
        assert wi.last_checked_at == NOW
