"""Tests for InfoItem picker dashboard routes (#162)."""

import pytest
from archiver_client import ServerError

from tests.conftest import make_info_item

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
