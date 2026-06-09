"""Integration tests for the create_watch service function (#185 Phase A step 7).

Step 7: create_watch now accepts watched_item_id — the WatchedItem must already
exist. No Archiver SDK calls; no probe_fn. URL resolution is the WatchedItem's
responsibility (set at WatchedItem-create time).
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem
from src.core.watches import create_watch
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


async def _make_wi(db_session, *, name="Test WI", url="https://example.com/page"):
    """Create + flush a WatchedItem with effective_url; return it."""
    item = await make_info_item(db_session, name=name)
    wi = WatchedItem(
        archiver_info_item_id=item.info_item_id,
        name=name,
        effective_url=url,
    )
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestCreateWatch:
    async def test_returns_committed_watch(self, db_session):
        wi = await _make_wi(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            name="Test Watch",
            watched_item_id=str(wi.id),
            content_type="html",
        )
        assert isinstance(watch, Watch)
        assert watch.id is not None
        assert watch.name == "Test Watch"
        assert watch.watched_item_id == wi.id

    async def test_watch_linked_to_correct_wi(self, db_session):
        wi = await _make_wi(db_session)
        watch = await create_watch(
            session=db_session,
            name="Linked Watch",
            watched_item_id=str(wi.id),
        )
        await db_session.refresh(watch, ["watched_item"])
        assert watch.watched_item.id == wi.id

    async def test_raises_on_unknown_watched_item_id(self, db_session):
        from ulid import ULID

        with pytest.raises(ValueError, match="not found"):
            await create_watch(
                session=db_session,
                name="Orphan",
                watched_item_id=str(ULID()),
            )

    async def test_creates_audit_log(self, db_session):
        wi = await _make_wi(db_session, url="https://audit-test.com/page")
        watch = await create_watch(
            session=db_session,
            name="Audit Watch",
            watched_item_id=str(wi.id),
            content_type="html",
        )
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_CREATED,
                AuditLog.watch_id == str(watch.id),
            )
        )
        entry = result.scalar_one()
        assert entry.payload["name"] == "Audit Watch"
        assert entry.payload["watched_item_id"] == str(wi.id)

    async def test_dispatches_watch_created_event(self, db_session):
        wi = await _make_wi(db_session, url="https://notify-test.com/page")
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                name="Notify Watch",
                watched_item_id=str(wi.id),
                content_type="html",
            )
            mock_dispatch.assert_awaited_once()
            event = mock_dispatch.call_args.kwargs["event"]
            assert event.watch_id == str(watch.id)
            assert event.watch_url == "https://notify-test.com/page"

    async def test_optional_fields(self, db_session):
        wi = await _make_wi(db_session)
        watch = await create_watch(
            session=db_session,
            name="Optional Fields",
            watched_item_id=str(wi.id),
            description="a description",
            tags=["tag1", "tag2"],
        )
        assert watch.description == "a description"
        assert watch.tags == ["tag1", "tag2"]

    async def test_watch_url_in_event_falls_back_to_sentinel(self, db_session):
        """When WatchedItem has no effective_url, event uses watch: sentinel."""
        item = await make_info_item(db_session)
        wi = WatchedItem(
            archiver_info_item_id=item.info_item_id, name="No URL WI", effective_url=""
        )
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                name="No URL Watch",
                watched_item_id=str(wi.id),
            )
            event = mock_dispatch.call_args.kwargs["event"]
            assert event.watch_url == f"watch:{watch.id}"
