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

    async def test_select_only_result_has_binding_tree_htmx(self, client, db_session, info_client):
        """select_only buttons must carry hx-get to the binding-tree route so
        the hidden info_item_id input is injected when an option is chosen.
        Without it the WatchedItem create form can never submit a valid id."""
        await make_info_item(db_session, name="Picker Test Item")
        await db_session.commit()
        response = await client.get(
            "/info-items/search?q=picker&mode=select_only&target_form_id=wi-create"
        )
        assert response.status_code == 200
        body = response.text
        assert "binding-tree" in body
        assert "mode=select_only" in body
        assert "data-info-item-select" not in body


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

    async def test_unknown_info_item_returns_200_error_partial(self, client, info_client):
        """NotFound → 200 error partial so HTMX swaps the message into the target div."""
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.get("/info-items/01ZZZZZZZZZZZZZZZZZZZZZZZZ/binding-tree")
        assert response.status_code == 200
        assert "not found" in response.text.lower()

    async def test_select_with_target_no_primary_binding_returns_200_error(
        self, client, db_session, info_client
    ):
        """select_with_target + no primary binding → 200 error partial (HTMX must swap it)."""
        item = await make_info_item(db_session)
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_with_target"
        )
        assert response.status_code == 200
        assert "primary" in response.text.lower()

    async def test_select_only_no_primary_binding_renders_tree(
        self, client, db_session, info_client
    ):
        """select_only mode must render the binding tree even when the InfoItem has no
        primary binding — WI-create only needs info_item_id, not a watch target."""
        item = await make_info_item(db_session, name="No-Binding Item")
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=select_only&target_form_id=wi-create"
        )
        assert response.status_code == 200
        body = response.text
        assert "No-Binding Item" in body
        # Hidden input must be present so the WI-create form gets info_item_id
        assert f'value="{item.info_item_id}"' in body
        assert 'name="info_item_id"' in body

    async def test_readonly_tree_no_primary_binding_renders_tree(
        self, client, db_session, info_client
    ):
        """readonly_tree mode degrades gracefully when the InfoItem has no primary binding
        — same path as select_only; no radio controls, no hidden info_item_id input."""
        item = await make_info_item(db_session, name="Readonly No-Binding")
        await db_session.commit()
        response = await client.get(
            f"/info-items/{item.info_item_id}/binding-tree?mode=readonly_tree"
        )
        assert response.status_code == 200
        body = response.text
        assert "Readonly No-Binding" in body
        # readonly_tree never injects a hidden input
        assert 'name="info_item_id"' not in body
        assert "<input " not in body

    async def test_transport_error_returns_200_error_partial(self, client, info_client):
        """SDK transport error → 200 error partial so HTMX swaps the message."""
        info_client.get_info_item = AsyncMock(side_effect=ServerError("boom"))
        response = await client.get("/info-items/01ZZZZZZZZZZZZZZZZZZZZZZZZ/binding-tree")
        assert response.status_code == 200
        assert "unavailable" in response.text.lower()


class TestTypeaheadPartial:
    async def test_typeahead_renders_combobox_attributes(self, client, db_session, info_client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        body = response.text
        assert 'role="combobox"' in body
        assert "aria-expanded" in body
        assert "aria-activedescendant" in body
        assert 'hx-get="/info-items/search' in body
