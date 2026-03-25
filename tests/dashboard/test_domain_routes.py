"""Integration tests for domain dashboard routes."""

import pytest

from src.core.models.domain import Domain

pytestmark = pytest.mark.integration


class TestDomainsListPage:
    async def test_domains_page_returns_200(self, client):
        response = await client.get("/domains")
        assert response.status_code == 200
        assert b"Domains" in response.content

    async def test_domains_page_has_create_link(self, client):
        response = await client.get("/domains")
        assert b"/domains/new" in response.content

    async def test_domains_page_has_search_input(self, client):
        response = await client.get("/domains")
        assert b'name="q"' in response.content

    async def test_domains_page_has_filter_pills(self, client):
        response = await client.get("/domains")
        assert b"Active" in response.content
        assert b"Archived" in response.content

    async def test_domains_table_partial(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200

    async def test_domains_table_search(self, client, db_session):
        db_session.add(Domain(name="findme.com"))
        db_session.add(Domain(name="other.com"))
        await db_session.flush()
        response = await client.get("/partials/domains-table?q=findme&status=")
        assert response.status_code == 200
        assert b"findme.com" in response.content
        assert b"other.com" not in response.content

    async def test_domains_table_has_edit_button(self, client, db_session):
        db_session.add(Domain(name="editable.com"))
        await db_session.flush()
        response = await client.get("/partials/domains-table?status=")
        assert b"/domains/editable.com" in response.content

    async def test_domains_table_shows_last_checked(self, client):
        response = await client.get("/partials/domains-table")
        assert response.status_code == 200
        assert b"Last Checked" in response.content


class TestDomainCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/domains/new")
        assert response.status_code == 200
        assert b"New Domain" in response.content

    async def test_create_form_has_url_input(self, client):
        response = await client.get("/domains/new")
        assert b'name="url"' in response.content

    async def test_create_domain_redirects_to_detail(self, client):
        response = await client.post(
            "/domains",
            data={"url": "https://newdomain.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "newdomain.com" in response.headers["location"]

    async def test_create_domain_missing_url_shows_error(self, client):
        response = await client.post("/domains", data={"url": ""})
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()

    async def test_create_domain_duplicate_redirects_to_existing(self, client, db_session):
        db_session.add(Domain(name="existing.com"))
        await db_session.flush()
        response = await client.post(
            "/domains",
            data={"url": "https://existing.com/page"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "existing.com" in response.headers["location"]
