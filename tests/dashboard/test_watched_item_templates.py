"""Integration tests for WatchedItem notification-template UI."""

import pytest

pytestmark = pytest.mark.integration


async def _seed(db_session, name="WI"):
    from src.core.models.watched_item import WatchedItem
    from tests.conftest import make_info_item

    item = await make_info_item(db_session)
    wi = WatchedItem(info_item_id=item.info_item_id, name=name)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


async def _seed_tpl(db_session, watched_item):
    from src.core.models.watched_item_notification_template import (
        WatchedItemNotificationTemplate,
    )

    tpl = WatchedItemNotificationTemplate(
        watched_item_id=watched_item.id,
        title="Email",
        channel_hint="mailto://x:y@z",
    )
    db_session.add(tpl)
    await db_session.flush()
    await db_session.commit()
    return tpl


class TestTemplatesPartial:
    async def test_list_empty(self, client, db_session):
        wi = await _seed(db_session)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"No notification templates" in response.content

    async def test_list_renders_row(self, client, db_session):
        wi = await _seed(db_session)
        await _seed_tpl(db_session, wi)
        response = await client.get(
            f"/partials/watched-item-templates/{wi.id}",
            headers={"HX-Request": "true"},
        )
        assert b"Email" in response.content
        assert b"mailto" in response.content or b"channel" in response.content
