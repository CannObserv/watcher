"""Tests for the NotificationTemplate mutation service (#228).

The service is the single owner of the mutation + audit pairing for template
CRUD, shared by the API library surface, the API item-scoped surface, and the
dashboard routes. Audit payloads beyond ``template_id`` are caller-supplied
(``audit_fields``) so each surface keeps its established payload shape.
"""

import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.notification_template import (
    VISIBILITY_GLOBAL,
    NotificationTemplate,
)
from src.core.notifications.templates import (
    create_template,
    delete_template,
    duplicate_template,
    update_template_fields,
)

pytestmark = pytest.mark.integration


async def _last_audit(session, event_type: str) -> AuditLog:
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.event_type == event_type)
        # Tie-break on the id (CR-9): rows written in one transaction can share
        # a created_at, which made "the last one" nondeterministic and this
        # module intermittently red. The id is a ULID — monotonic within a
        # transaction — so it orders what the timestamp cannot separate.
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    rows = result.scalars().all()
    assert rows, f"no audit rows for {event_type}"
    return rows[-1]


class TestCreateTemplate:
    async def test_persists_row_and_audits_with_extra_fields(self, db_session):
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="Ops list",
            channel_hint="email",
            events=["change_detected"],
            remote_channel_id="chan-1",
            audit_fields={"source": "dashboard", "title": "Ops list"},
        )
        assert tpl.id is not None
        assert tpl.visibility == VISIBILITY_GLOBAL
        row = await _last_audit(db_session, EventType.NOTIFICATION_TEMPLATE_CREATED)
        assert row.payload["template_id"] == str(tpl.id)
        assert row.payload["source"] == "dashboard"
        assert row.payload["title"] == "Ops list"


class TestUpdateTemplateFields:
    async def test_sets_only_given_fields_and_audits(self, db_session):
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="Before",
            channel_hint="email",
            events=["change_detected"],
            remote_channel_id="chan-1",
            audit_fields={},
        )
        update_template_fields(
            db_session,
            tpl,
            {"title": "After", "is_active": False},
            audit_fields={"source": "api"},
        )
        await db_session.flush()
        assert tpl.title == "After"
        assert tpl.is_active is False
        assert tpl.channel_hint == "email"  # untouched
        row = await _last_audit(db_session, EventType.NOTIFICATION_TEMPLATE_UPDATED)
        assert row.payload["template_id"] == str(tpl.id)
        assert row.payload["source"] == "api"

    async def test_content_config_can_be_set_to_none(self, db_session):
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="T",
            channel_hint="email",
            events=["change_detected"],
            content_config={"default": {"include_domain": True}},
            remote_channel_id="chan-1",
            audit_fields={},
        )
        update_template_fields(db_session, tpl, {"content_config": None}, audit_fields={})
        await db_session.flush()
        assert tpl.content_config is None


class TestDeleteTemplate:
    async def test_removes_row_and_audits(self, db_session):
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="Doomed",
            channel_hint="email",
            events=["change_detected"],
            remote_channel_id="chan-1",
            audit_fields={},
        )
        tpl_id = str(tpl.id)
        await delete_template(db_session, tpl, audit_fields={"source": "dashboard"})
        await db_session.flush()
        remaining = await db_session.execute(
            select(NotificationTemplate).where(NotificationTemplate.id == tpl.id)
        )
        assert remaining.scalar_one_or_none() is None
        row = await _last_audit(db_session, EventType.NOTIFICATION_TEMPLATE_DELETED)
        assert row.payload["template_id"] == tpl_id

    async def test_delete_audits_before_removal(self, db_session):
        """Audit row must survive the delete (id captured before removal)."""
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="X",
            channel_hint="email",
            events=["change_detected"],
            remote_channel_id="chan-1",
            audit_fields={},
        )
        await delete_template(db_session, tpl, audit_fields={})
        await db_session.commit()
        row = await _last_audit(db_session, EventType.NOTIFICATION_TEMPLATE_DELETED)
        assert row.payload["template_id"]


class TestDuplicateTemplate:
    async def test_copies_fields_with_copy_suffix_and_audits_real_id(self, db_session):
        tpl = await create_template(
            db_session,
            visibility=VISIBILITY_GLOBAL,
            title="Original",
            channel_hint="email",
            events=["change_detected", "watch_error"],
            content_config={"default": {"include_tags": True}},
            remote_channel_id="chan-9",
            audit_fields={},
        )
        copy = await duplicate_template(db_session, tpl, audit_fields={"source": "dashboard"})
        await db_session.flush()
        assert copy.id != tpl.id
        assert copy.title == "Original (copy)"
        assert copy.events == tpl.events
        assert copy.events is not tpl.events  # independent list
        assert copy.remote_channel_id == "chan-9"
        assert copy.visibility == tpl.visibility
        row = await _last_audit(db_session, EventType.NOTIFICATION_TEMPLATE_CREATED)
        assert row.payload["template_id"] == str(copy.id)
