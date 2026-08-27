"""Tests for the unacknowledged-specs signal (#274).

``last_reviewed_at`` was an orphan: the column and both ``mark-reviewed``
handlers existed, the detail page displayed the timestamp, and nothing on Earth
could set it — #185 Phase A removed the banner that held the only form, and the
binding list it was meant to be compared against became unobtainable when #254
took away Watcher's last HTTP call to Archiver.

#274 repurposes it rather than dropping it: the timestamp now means "the operator
has acknowledged the current ``source_specs``", compared against the newest
``watched_item.announcement_applied`` event whose diff touched ``source_specs``.

Deliberately narrow. A cadence re-announcement does **not** raise the flag —
``announced_schedule_config`` changes when the item is checked, not what the
fingerprint means, and an acknowledgement prompt that fires for it would be
trained away.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.watched_item import WatchedItem
from src.dashboard.context import unacknowledged_spec_change
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


async def _wi(db_session, **kwargs):
    item = await make_info_item(db_session)
    wi = WatchedItem(
        archiver_info_source_id=str(ULID()),
        archiver_info_item_id=item.info_item_id,
        name=kwargs.pop("name", "Ack"),
        **kwargs,
    )
    db_session.add(wi)
    await db_session.commit()
    return wi


async def _spec_event(db_session, wi, *, at, changes=None):
    row = AuditLog(
        event_type=EventType.WATCHED_ITEM_ANNOUNCEMENT_APPLIED,
        payload={
            "watched_item_id": str(wi.id),
            "changes": changes
            if changes is not None
            else {"source_specs": {"old": [{"selector": "#a"}], "new": [{"selector": "#b"}]}},
        },
        created_at=at,
    )
    db_session.add(row)
    await db_session.commit()
    return row


class TestUnacknowledgedSpecChange:
    async def test_no_events_is_nothing_to_acknowledge(self, db_session):
        wi = await _wi(db_session)

        assert await unacknowledged_spec_change(db_session, wi) is None

    async def test_a_spec_change_on_a_never_reviewed_item_is_unacknowledged(self, db_session):
        """``last_reviewed_at IS NULL`` is "never acknowledged", not "acknowledged
        at the dawn of time" — the null had to mean one of the two and the safe
        direction is to surface it."""
        wi = await _wi(db_session)
        await _spec_event(db_session, wi, at=NOW)

        assert await unacknowledged_spec_change(db_session, wi) == NOW

    async def test_a_change_before_the_review_is_acknowledged(self, db_session):
        wi = await _wi(db_session, last_reviewed_at=NOW)
        await _spec_event(db_session, wi, at=NOW - timedelta(days=1))

        assert await unacknowledged_spec_change(db_session, wi) is None

    async def test_a_change_after_the_review_is_unacknowledged(self, db_session):
        wi = await _wi(db_session, last_reviewed_at=NOW - timedelta(days=1))
        await _spec_event(db_session, wi, at=NOW)

        assert await unacknowledged_spec_change(db_session, wi) == NOW

    async def test_the_newest_change_wins(self, db_session):
        """Two spec changes since the last review is still one prompt, dated to
        the most recent — the operator acknowledges the current specs, not a
        queue of past edits."""
        wi = await _wi(db_session)
        await _spec_event(db_session, wi, at=NOW - timedelta(days=2))
        await _spec_event(db_session, wi, at=NOW)

        assert await unacknowledged_spec_change(db_session, wi) == NOW

    async def test_a_cadence_only_change_does_not_count(self, db_session):
        """Narrowed on purpose: cadence changes when the item is checked, not
        what the fingerprint means."""
        wi = await _wi(db_session)
        await _spec_event(
            db_session,
            wi,
            at=NOW,
            changes={"announced_schedule_config": {"old": None, "new": {"interval": "6h"}}},
        )

        assert await unacknowledged_spec_change(db_session, wi) is None

    async def test_a_mixed_change_touching_specs_counts(self, db_session):
        wi = await _wi(db_session)
        await _spec_event(
            db_session,
            wi,
            at=NOW,
            changes={
                "announced_schedule_config": {"old": None, "new": {"interval": "6h"}},
                "source_specs": {"old": [], "new": [{"selector": "#b"}]},
            },
        )

        assert await unacknowledged_spec_change(db_session, wi) == NOW

    async def test_another_items_change_does_not_leak(self, db_session):
        wi = await _wi(db_session, name="Mine")
        other = await _wi(db_session, name="Theirs")
        await _spec_event(db_session, other, at=NOW)

        assert await unacknowledged_spec_change(db_session, wi) is None


class TestDetailPageSurface:
    """The audit row makes the change *traceable*; the badge makes it *noticed*.

    Without this, surfacing amounts to a line in Recent Activity that nobody
    scrolls to — which is the failure #274 was filed about, one layer down.
    """

    async def test_the_badge_appears_on_an_unacknowledged_change(self, client, db_session):
        wi = await _wi(db_session, name="Badged")
        await _spec_event(db_session, wi, at=NOW)

        body = (await client.get(f"/watched-items/{wi.id}")).text

        assert "Specs changed" in body
        assert "2026-08-27" in body

    async def test_the_badge_is_absent_when_acknowledged(self, client, db_session):
        wi = await _wi(db_session, name="Quiet", last_reviewed_at=NOW)
        await _spec_event(db_session, wi, at=NOW - timedelta(days=1))

        body = (await client.get(f"/watched-items/{wi.id}")).text

        assert "Specs changed" not in body

    async def test_the_acknowledge_button_targets_the_existing_route(self, client, db_session):
        """The route has existed and been tested since #161; it has only ever
        lacked a caller."""
        wi = await _wi(db_session, name="Ackable")
        await _spec_event(db_session, wi, at=NOW)

        body = (await client.get(f"/watched-items/{wi.id}")).text

        assert f"/watched-items/{wi.id}/mark-reviewed" in body

    async def test_acknowledging_clears_the_badge(self, client, db_session):
        wi = await _wi(db_session, name="Roundtrip")
        await _spec_event(db_session, wi, at=NOW)

        await client.post(f"/watched-items/{wi.id}/mark-reviewed", follow_redirects=False)

        body = (await client.get(f"/watched-items/{wi.id}")).text
        assert "Specs changed" not in body
