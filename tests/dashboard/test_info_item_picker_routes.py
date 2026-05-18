"""Tests for InfoItem picker dashboard routes (#162)."""

import pytest

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
