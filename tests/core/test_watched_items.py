"""Tests for the WatchedItem activation service (#228).

``set_watched_item_active`` is the single owner of the pause/resume business
rules — archived guard, resume-while-suspended guard, and the dedicated
PAUSED/RESUMED audit events — shared by the API PATCH path and the dashboard
toggle.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.watched_items import (
    ArchivedItemActivationError,
    SuspendedDomainResumeError,
    set_watched_item_active,
)
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration


async def _audit_events(session, watched_item_id: str) -> list[str]:
    result = await session.execute(select(AuditLog).order_by(AuditLog.created_at))
    return [
        row.event_type
        for row in result.scalars().all()
        if row.payload and row.payload.get("watched_item_id") == watched_item_id
    ]


class TestSetWatchedItemActive:
    async def test_pause_transition_sets_flag_and_audits(self, db_session):
        wi = await make_watched_item(db_session, auto_info_item=False, is_active=True)
        changed = set_watched_item_active(db_session, wi, active=False, source="api")
        await db_session.flush()
        assert changed is True
        assert wi.is_active is False
        assert await _audit_events(db_session, str(wi.id)) == [EventType.WATCHED_ITEM_PAUSED]

    async def test_resume_transition_sets_flag_and_audits(self, db_session):
        wi = await make_watched_item(db_session, auto_info_item=False, is_active=False)
        changed = set_watched_item_active(db_session, wi, active=True, source="dashboard")
        await db_session.flush()
        assert changed is True
        assert wi.is_active is True
        assert await _audit_events(db_session, str(wi.id)) == [EventType.WATCHED_ITEM_RESUMED]

    async def test_noop_returns_false_and_emits_no_audit(self, db_session):
        wi = await make_watched_item(db_session, auto_info_item=False, is_active=True)
        changed = set_watched_item_active(db_session, wi, active=True, source="api")
        await db_session.flush()
        assert changed is False
        assert await _audit_events(db_session, str(wi.id)) == []

    async def test_archived_item_rejects_any_change(self, db_session):
        wi = await make_watched_item(
            db_session,
            auto_info_item=False,
            is_active=False,
            archived_at=datetime.now(UTC),
        )
        with pytest.raises(ArchivedItemActivationError):
            set_watched_item_active(db_session, wi, active=True, source="api")
        # Even a would-be no-op is rejected: restore owns activation while archived.
        with pytest.raises(ArchivedItemActivationError):
            set_watched_item_active(db_session, wi, active=False, source="api")

    async def test_resume_blocked_while_domain_suspended(self, db_session):
        wi = await make_watched_item(
            db_session, auto_info_item=False, is_active=False, domain_suspended=True
        )
        with pytest.raises(SuspendedDomainResumeError):
            set_watched_item_active(db_session, wi, active=True, source="dashboard")
        assert wi.is_active is False

    async def test_pause_allowed_while_domain_suspended(self, db_session):
        wi = await make_watched_item(
            db_session, auto_info_item=False, is_active=True, domain_suspended=True
        )
        changed = set_watched_item_active(db_session, wi, active=False, source="api")
        await db_session.flush()
        assert changed is True
        assert wi.is_active is False

    async def test_audit_carries_source(self, db_session):
        wi = await make_watched_item(db_session, auto_info_item=False, is_active=True)
        set_watched_item_active(db_session, wi, active=False, source="dashboard")
        await db_session.flush()
        result = await db_session.execute(select(AuditLog).order_by(AuditLog.created_at))
        rows = [
            r
            for r in result.scalars().all()
            if r.payload and r.payload.get("watched_item_id") == str(wi.id)
        ]
        assert rows and rows[-1].payload.get("source") == "dashboard"
