"""Tests for InfoItem picker dashboard routes (#162)."""

from unittest.mock import AsyncMock

import pytest
from archiver_client import NotFound, ServerError

from tests._information_test_models import InfoItemSource
from tests.conftest import (
    bind_primary_source,
    bind_sub_aspect,
    make_info_item,
    make_info_source,
)

pytestmark = pytest.mark.integration


class TestFindInfoItemFixture:
    async def test_fake_client_find_returns_db_matches(self, db_session, info_client):
        await make_info_item(db_session, name="Alpha Item")
        await make_info_item(db_session, name="Bravo Item")
        await db_session.commit()
        results = await info_client.find_info_item("alpha")
        assert any(r.name == "Alpha Item" for r in results)
        assert not any(r.name == "Bravo Item" for r in results)


class TestSearchRoute:
    async def test_search_returns_results_partial(self, client, db_session, info_client):
        await make_info_item(db_session, name="LCB Annual Report")
        await db_session.commit()
        response = await client.get("/info-items/search?q=lcb&mode=select_with_target")
        assert response.status_code == 200
        body = response.text
        assert "LCB Annual Report" in body
        # role="option" makes the row a combobox option
        assert 'role="option"' in body

    async def test_search_no_results_renders_empty_state(self, client, info_client):
        response = await client.get("/info-items/search?q=zzzzzzzz")
        assert response.status_code == 200
        assert "No matches" in response.text

    async def test_search_empty_query_returns_no_results(self, client, info_client):
        # Don't fan out to the SDK on an empty query — render the empty hint.
        response = await client.get("/info-items/search?q=")
        assert response.status_code == 200
        info_client.find_info_item.assert_not_called()

    async def test_sdk_server_error_degrades_to_empty_results(self, client, info_client):
        info_client.find_info_item.side_effect = ServerError("boom")
        response = await client.get("/info-items/search?q=anything")
        assert response.status_code == 200
        assert "No matches" in response.text

    async def test_search_limit_capped_at_20(self, client, info_client, db_session):
        for i in range(25):
            await make_info_item(db_session, name=f"Item {i:02d}")
        await db_session.commit()
        await client.get("/info-items/search?q=Item")
        # SDK is called with limit=20 (the design's recommended bound)
        args, kwargs = info_client.find_info_item.call_args
        assert kwargs.get("limit") == 20


class TestBindingTreeRoute:
    async def test_renders_primary_only(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="X")
        primary = await make_info_source(db_session, url="https://example.com/p")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        assert response.status_code == 200
        body = response.text
        assert "primary" in body.lower()
        assert "https://example.com/p" in body

    async def test_renders_sub_aspect_selectable(self, client, db_session, info_client):
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        sub = await make_info_source(db_session, parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=sub.info_source_id,
        )
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        body = response.text
        # sub_aspect row is selectable
        assert "sub_aspect" in body
        assert str(sub.info_source_id) in body
        # selectable controls show value attributes with the sub_aspect id
        assert 'value="' + str(sub.info_source_id) + '"' in body

    async def test_cross_check_muted(self, client, db_session, info_client):
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        cc = await make_info_source(db_session, parent_info_source_id=primary.info_source_id)
        db_session.add(
            InfoItemSource(
                info_item_id=item.info_item_id,
                info_source_id=cc.info_source_id,
                role="cross_check",
            )
        )
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        body = response.text
        assert "cross_check" in body
        # cross_check is NOT exposed as a selectable form value
        assert 'value="' + str(cc.info_source_id) + '"' not in body

    async def test_readonly_mode_omits_form_controls(self, client, db_session, info_client):
        item = await make_info_item(db_session)
        primary = await make_info_source(db_session, url="https://example.com")
        await bind_primary_source(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=primary.info_source_id,
        )
        sub = await make_info_source(db_session, parent_info_source_id=primary.info_source_id)
        await bind_sub_aspect(
            db_session,
            info_item_id=item.info_item_id,
            info_source_id=sub.info_source_id,
        )
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=readonly_tree"
        )
        body = response.text
        assert "sub_aspect" in body
        # Readonly: no <input>/<button type=radio>/select controls
        assert "<input " not in body
        assert 'type="radio"' not in body

    async def test_404_unknown_info_item(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.get("/info-items/01ZZZZZZZZZZZZZZZZZZZZZZZZ/binding-tree")
        assert response.status_code == 404
