"""Tests for the host-level suspension service (#372).

``set_domain_active`` is the one writer of ``Domain.is_active`` and the
``WatchedItem.domain_suspended`` back-fill that follows it. The dashboard toggle
and the break-glass CLI both delegate to it, so the CLI never imports the
dashboard (#377 deletes it) and the two cannot drift.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.domain_suspension import (
    DomainArchived,
    DomainNotFound,
    set_domain_active,
)
from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration


async def _audits(session, name: str) -> list[AuditLog]:
    rows = (await session.execute(select(AuditLog))).scalars().all()
    return [r for r in rows if r.payload.get("domain_name") == name]


async def _item(session, host: str, **kwargs):
    return await make_watched_item(
        session,
        primary_url=f"https://{host}/{kwargs.pop('path', 'p')}",
        domain_name=host,
        **kwargs,
    )


class TestSuspend:
    async def test_deactivates_the_domain(self, db_session):
        db_session.add(Domain(name="s1.example"))
        await db_session.flush()

        result = await set_domain_active(db_session, "s1.example", active=False, source="cli")

        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "s1.example"))
        ).scalar_one()
        assert domain.is_active is False
        assert domain.is_suspended is True
        assert result.changed is True

    async def test_flags_every_item_on_the_host_whatever_its_own_state(self, db_session):
        db_session.add(Domain(name="s2.example"))
        live = await _item(db_session, "s2.example", path="a", is_active=True)
        paused = await _item(db_session, "s2.example", path="b", is_active=False)
        archived = await _item(
            db_session, "s2.example", path="c", is_active=False, archived_at=datetime.now(UTC)
        )

        result = await set_domain_active(db_session, "s2.example", active=False, source="cli")

        for wi in (live, paused, archived):
            await db_session.refresh(wi)
            assert wi.domain_suspended is True
        assert result.items_updated == 3

    async def test_leaves_other_hosts_alone(self, db_session):
        db_session.add_all([Domain(name="s3.example"), Domain(name="other-s3.example")])
        bystander = await _item(db_session, "other-s3.example")

        await set_domain_active(db_session, "s3.example", active=False, source="cli")

        await db_session.refresh(bystander)
        assert bystander.domain_suspended is False

    async def test_audits_the_change_with_its_source(self, db_session):
        db_session.add(Domain(name="s4.example"))
        await db_session.flush()

        await set_domain_active(db_session, "s4.example", active=False, source="cli")
        await db_session.flush()

        [entry] = await _audits(db_session, "s4.example")
        assert entry.event_type == EventType.DOMAIN_DEACTIVATED
        assert entry.payload == {"domain_name": "s4.example", "source": "cli"}


class TestResume:
    async def test_reactivates_and_clears_the_flag(self, db_session):
        db_session.add(Domain(name="r1.example", is_active=False))
        wi = await _item(db_session, "r1.example", domain_suspended=True)

        result = await set_domain_active(db_session, "r1.example", active=True, source="cli")

        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "r1.example"))
        ).scalar_one()
        await db_session.refresh(wi)
        assert domain.is_active is True
        assert wi.domain_suspended is False
        assert result.changed is True

    async def test_does_not_unpause_an_item_paused_on_its_own(self, db_session):
        """Host suspension and item pause are separate levers: resuming the host
        clears only the former."""
        db_session.add(Domain(name="r2.example", is_active=False))
        wi = await _item(db_session, "r2.example", is_active=False, domain_suspended=True)

        await set_domain_active(db_session, "r2.example", active=True, source="cli")

        await db_session.refresh(wi)
        assert wi.is_active is False

    async def test_audits_the_change_with_its_source(self, db_session):
        db_session.add(Domain(name="r3.example", is_active=False))
        await db_session.flush()

        await set_domain_active(db_session, "r3.example", active=True, source="dashboard")
        await db_session.flush()

        [entry] = await _audits(db_session, "r3.example")
        assert entry.event_type == EventType.DOMAIN_ACTIVATED
        assert entry.payload == {"domain_name": "r3.example", "source": "dashboard"}


class TestNoOp:
    @pytest.mark.parametrize("active", [False, True])
    async def test_the_current_state_writes_no_audit_row(self, db_session, active):
        name = f"noop-{active}.example".lower()
        db_session.add(Domain(name=name, is_active=active))
        await db_session.flush()

        result = await set_domain_active(db_session, name, active=active, source="cli")
        await db_session.flush()

        assert result.changed is False
        assert await _audits(db_session, name) == []

    async def test_an_already_suspended_host_still_reasserts_the_flag(self, db_session):
        """ "Already suspended" must be true of the items too, not only the row: an
        item that drifted is re-flagged rather than left fetching."""
        db_session.add(Domain(name="drift.example", is_active=False))
        drifted = await _item(db_session, "drift.example", path="a", domain_suspended=False)
        await _item(db_session, "drift.example", path="b", domain_suspended=True)

        result = await set_domain_active(db_session, "drift.example", active=False, source="cli")

        await db_session.refresh(drifted)
        assert drifted.domain_suspended is True
        assert result.changed is False
        assert result.items_updated == 1


class TestRefusals:
    async def test_an_unknown_host_raises(self, db_session):
        with pytest.raises(DomainNotFound):
            await set_domain_active(db_session, "absent.example", active=False, source="cli")

    @pytest.mark.parametrize("active", [False, True])
    async def test_an_archived_host_raises_and_writes_nothing(self, db_session, active):
        """Archived already suspends; flipping ``is_active`` under it would make
        restore resurrect a state nobody chose."""
        name = f"arch-{active}.example".lower()
        db_session.add(Domain(name=name, is_active=True, archived_at=datetime.now(UTC)))
        wi = await _item(db_session, name, domain_suspended=True)

        with pytest.raises(DomainArchived):
            await set_domain_active(db_session, name, active=active, source="cli")

        await db_session.refresh(wi)
        assert wi.domain_suspended is True
        assert await _audits(db_session, name) == []
