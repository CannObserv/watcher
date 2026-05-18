"""Integration tests for WatchedItem auto-create audit (#162)."""

import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.probe import ProbeResult
from src.core.watches import create_watch
from tests.conftest import bind_primary_source, make_info_item, make_info_source

pytestmark = pytest.mark.integration


class TestWatchedItemAutoCreateAudit:
    async def test_auto_create_emits_audit(self, db_session, info_client):
        item = await make_info_item(db_session, name="A")
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        await db_session.commit()

        async def fake_probe(url):
            return ProbeResult(
                effective_url=url,
                effective_domain="example.com",
                redirect_chain=[url],
                status_code=200,
                content_type="text/html",
            )

        await create_watch(
            session=db_session,
            probe_fn=fake_probe,
            info_client=info_client,
            name="W",
            info_item_id=str(item.info_item_id),
        )
        events = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1, f"expected 1 WATCHED_ITEM_CREATED audit row, got {len(events)}"
        assert events[0].payload["source"] == "auto_create"
        assert events[0].payload["info_item_id"] == str(item.info_item_id)
