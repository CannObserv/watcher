"""Tests for domain field edit/save/cancel workflow."""

import pytest

from src.core.models.domain import Domain

pytestmark = pytest.mark.integration


class TestDomainFieldPartialGet:
    """GET /domains/{name}/field/{field_name} — serves field partial."""

    async def test_field_partial_view_mode_default(self, client, db_session):
        db_session.add(Domain(name="field.com", min_interval=2.5))
        await db_session.flush()
        response = await client.get(
            "/domains/field.com/field/min_interval",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"2.5" in response.content
        assert b"<input" not in response.content
        assert b"Edit" in response.content

    async def test_field_partial_edit_mode(self, client, db_session):
        db_session.add(Domain(name="edit-mode.com", min_interval=2.5))
        await db_session.flush()
        response = await client.get(
            "/domains/edit-mode.com/field/min_interval?mode=edit",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"disabled" not in response.content
        assert b"Save" in response.content
        assert b"Cancel" in response.content

    async def test_field_partial_textarea_view_mode(self, client, db_session):
        db_session.add(Domain(name="notes-view.com", notes="Some notes"))
        await db_session.flush()
        response = await client.get(
            "/domains/notes-view.com/field/notes",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Some notes" in response.content
        assert b"<textarea" not in response.content
        assert b"Edit" in response.content

    async def test_field_partial_textarea_edit_mode(self, client, db_session):
        db_session.add(Domain(name="notes-edit.com", notes="Some notes"))
        await db_session.flush()
        response = await client.get(
            "/domains/notes-edit.com/field/notes?mode=edit",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"disabled" not in response.content
        assert b"Save" in response.content
        assert b"Cancel" in response.content

    async def test_field_partial_invalid_field_returns_400(self, client, db_session):
        db_session.add(Domain(name="bad.com"))
        await db_session.flush()
        response = await client.get("/domains/bad.com/field/name")
        assert response.status_code == 400

    async def test_field_partial_nonexistent_domain_returns_404(self, client):
        response = await client.get("/domains/nope.com/field/min_interval")
        assert response.status_code == 404

    async def test_field_partial_non_htmx_redirects(self, client, db_session):
        db_session.add(Domain(name="nohtmx.com"))
        await db_session.flush()
        response = await client.get(
            "/domains/nohtmx.com/field/min_interval",
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestDomainDetailFieldViewMode:
    """Domain detail page renders fields in view mode by default."""

    async def test_detail_page_fields_show_edit_button_in_view_mode(self, client, db_session):
        db_session.add(Domain(name="viewmode.com"))
        await db_session.flush()
        response = await client.get("/domains/viewmode.com")
        assert response.status_code == 200
        assert b"disabled" not in response.content
        assert b"Edit" in response.content

    async def test_detail_page_has_no_save_cancel_initially(self, client, db_session):
        db_session.add(Domain(name="nosave.com"))
        await db_session.flush()
        response = await client.get("/domains/nosave.com")
        content = response.content.decode()
        # Save/Cancel buttons should not appear in view mode
        # (noscript Save is still there for no-JS fallback)
        assert "Cancel" not in content


class TestDomainInlineUpdateReturnsViewMode:
    """POST /domains/{name} returns field in view mode after save."""

    async def test_save_returns_view_mode(self, client, db_session):
        db_session.add(Domain(name="save-view.com"))
        await db_session.flush()
        response = await client.post(
            "/domains/save-view.com",
            data={"field": "min_interval", "value": "5.0"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"<input" not in response.content
        assert b"Edit" in response.content
        assert b"5.0" in response.content
