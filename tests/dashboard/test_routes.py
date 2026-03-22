"""Integration tests for dashboard routes."""

import pytest

pytestmark = pytest.mark.integration


class TestDashboardHome:
    async def test_home_returns_200(self, client):
        response = await client.get("/")
        assert response.status_code == 200

    async def test_home_contains_title(self, client):
        response = await client.get("/")
        assert b"watcher" in response.content.lower()

    async def test_home_contains_nav(self, client):
        response = await client.get("/")
        assert b"Dashboard" in response.content
        assert b"Watches" in response.content


class TestPartialEndpoints:
    async def test_stats_cards_partial(self, client):
        response = await client.get("/partials/stats-cards")
        assert response.status_code == 200
        assert b"Total Watches" in response.content

    async def test_recent_changes_partial(self, client):
        response = await client.get("/partials/recent-changes")
        assert response.status_code == 200

    async def test_system_health_partial(self, client):
        response = await client.get("/partials/system-health")
        assert response.status_code == 200
        assert b"Task Queue" in response.content


class TestWatchList:
    async def test_watches_page_returns_200(self, client):
        response = await client.get("/watches")
        assert response.status_code == 200
        assert b"Watches" in response.content

    async def test_watches_page_has_create_link(self, client):
        response = await client.get("/watches")
        assert b"/watches/new" in response.content

    async def test_watch_table_partial(self, client):
        response = await client.get("/partials/watch-table")
        assert response.status_code == 200

    async def test_watch_table_filter(self, client):
        response = await client.get("/partials/watch-table?is_active=true")
        assert response.status_code == 200


class TestWatchDetail:
    async def test_detail_page_returns_200(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Detail Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}")
        assert response.status_code == 200
        assert b"Detail Watch" in response.content

    async def test_detail_page_404_invalid(self, client):
        response = await client.get("/watches/not-a-ulid")
        assert response.status_code == 404

    async def test_detail_page_has_edit_link(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Edit Link",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}")
        assert f"/watches/{watch_id}/edit".encode() in response.content


class TestWatchCreate:
    async def test_create_form_returns_200(self, client):
        response = await client.get("/watches/new")
        assert response.status_code == 200
        assert b"New Watch" in response.content

    async def test_create_form_has_fields(self, client):
        response = await client.get("/watches/new")
        assert b'name="name"' in response.content
        assert b'name="url"' in response.content
        assert b'name="content_type"' in response.content

    async def test_create_watch_redirects(self, client):
        response = await client.post(
            "/watches/new",
            data={
                "name": "Created Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def test_create_watch_missing_name_shows_error(self, client):
        response = await client.post(
            "/watches/new",
            data={
                "name": "",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        assert response.status_code == 200
        assert b"required" in response.content.lower() or b"error" in response.content.lower()


class TestWatchEdit:
    async def test_edit_form_returns_200(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Editable",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}/edit")
        assert response.status_code == 200
        assert b"Editable" in response.content

    async def test_edit_form_prefills(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Prefilled",
                "url": "https://prefilled.com",
                "content_type": "pdf",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.get(f"/watches/{watch_id}/edit")
        assert b"Prefilled" in response.content
        assert b"https://prefilled.com" in response.content

    async def test_edit_watch_redirects(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "ToEdit",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.post(
            f"/watches/{watch_id}/edit",
            data={
                "name": "Edited Name",
                "url": "https://edited.com",
                "content_type": "html",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestWatchDeactivate:
    async def test_deactivate_returns_updated_row(self, client):
        resp = await client.post(
            "/api/watches",
            json={
                "name": "Deactivate Me",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = resp.json()["id"]
        response = await client.post(f"/watches/{watch_id}/deactivate")
        assert response.status_code == 200
        assert b"Inactive" in response.content
