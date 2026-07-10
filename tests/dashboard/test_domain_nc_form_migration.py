"""Tests verifying the domain-default template create form renders the new
notification form sections (Content card + Per-event overrides + Preview pane),
and that content_config round-trips through create + error re-render (Step 5).

After Phase 5 (#137), forms post `remote_channel_id` rather than an
Apprise URL.
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from ulid import ULID

from src.core.models.domain import Domain
from src.core.models.notification_template import NotificationTemplate

VALID_CHANNEL_ID = str(ULID())


async def _ensure_domain(db_session, name: str) -> Domain:
    domain = Domain(name=name, is_active=True)
    db_session.add(domain)
    await db_session.flush()
    return domain


@pytest.mark.integration
class TestDomainNcCreatePersistsContentConfig:
    async def test_create_stores_content_config(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                "title": "Domain Alert",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
                "content_config__include_domain": "1",
                "content_config__body_template": "Custom: {{ item_url }}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        tpl = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.title == "Domain Alert")
        )
        assert tpl is not None
        assert tpl.content_config is not None
        assert tpl.content_config["default"]["include_domain"] is True
        assert tpl.content_config["default"]["body_template"] == "Custom: {{ item_url }}"

    async def test_create_without_toggles_stores_null_content_config(
        self, client: AsyncClient, db_session
    ):
        """`_parse_content_config_from_form` returns None when no toggles or
        templates are set; NotificationTemplate.content_config stays null."""
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                "title": "Plain Domain Alert",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        tpl = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.title == "Plain Domain Alert")
        )
        assert tpl is not None
        assert tpl.content_config is None


@pytest.mark.integration
class TestDomainNcCreateErrorPathPreservesContentConfig:
    async def test_missing_title_preserves_toggles(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                # title intentionally omitted — triggers validation error
                "remote_channel_id": VALID_CHANNEL_ID,
                "events": ["change_detected"],
                "content_config__include_domain": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert re.search(
            r'name="content_config__include_domain"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_domain should persist through the error re-render"


@pytest.mark.integration
class TestDomainNcNewPage:
    async def test_new_page_loads(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.get("/domains/example.com/notifications/new")
        assert resp.status_code == 200
        assert b"remote_channel_id" in resp.content
        assert b"example.com" in resp.content

    async def test_create_redirects_on_success(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                "title": "Domain Alert",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/domains/example.com" in resp.headers["location"]

    async def test_create_rerenders_page_on_error(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                "title": "",
                "remote_channel_id": VALID_CHANNEL_ID,
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"Title is required" in resp.content
