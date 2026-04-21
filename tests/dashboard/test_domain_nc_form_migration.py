"""Tests verifying the domain-default template create form renders the new
notification form sections (Content card + Per-event overrides + Preview pane),
and that content_config round-trips through create + error re-render (Step 5).
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.core.models.domain import Domain
from src.core.models.notification_template import NotificationTemplate
from src.core.notifications.events import WatchEventType


async def _ensure_domain(db_session, name: str) -> Domain:
    domain = Domain(name=name, is_active=True)
    db_session.add(domain)
    await db_session.flush()
    return domain


@pytest.mark.integration
class TestDomainNcAddRowMigrated:
    async def test_content_card_present(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.get(
            "/domains/example.com/nc-defaults/add-template-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # form_id sanitises dots → dashes for valid DOM ids / HTMX selectors
        assert 'id="content-h-dom-nc-example-com"' in resp.text
        assert 'id="overrides-h-dom-nc-example-com"' in resp.text
        assert 'id="preview-h-dom-nc-example-com"' in resp.text

    async def test_no_legacy_content_options_summary(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.get(
            "/domains/example.com/nc-defaults/add-template-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert not re.search(r"<summary[^>]*>\s*Content Options\s*</summary>", resp.text)

    async def test_all_eight_event_types_offered(self, client: AsyncClient, db_session):
        """Regression: the domain form must offer every WatchEventType as a
        subscribe checkbox, otherwise the override picker silently hides
        entire event classes from domain templates."""
        await _ensure_domain(db_session, "example.com")
        resp = await client.get(
            "/domains/example.com/nc-defaults/add-template-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        for et in WatchEventType:
            assert f'value="{et.value}"' in resp.text, (
                f"domain form missing event checkbox for {et.value}"
            )

    async def test_preview_pane_has_explicit_self_target(self, client: AsyncClient, db_session):
        """Regression against the hotfix — domain-default form must also set
        hx-target='this' on its preview pane, else the outer
        hx-target="#domain-nc-defaults" form attribute takes over."""
        await _ensure_domain(db_session, "example.com")
        resp = await client.get(
            "/domains/example.com/nc-defaults/add-template-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        pane_tag = re.compile(
            r'<div[^>]*id="nf-preview-pane-dom-nc-example-com"[^>]*\bhx-target="this"',
            re.DOTALL,
        )
        assert pane_tag.search(resp.text), (
            "preview pane is missing hx-target='this' on domain-default form"
        )


@pytest.mark.integration
class TestDomainNcCreatePersistsContentConfig:
    async def test_create_stores_content_config(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/nc-defaults/new",
            data={
                "title": "Domain Alert",
                "apprise_url": "json://hooks.example.com/notify",
                "events": ["change_detected"],
                "content_config__include_domain": "1",
                "content_config__body_template": "Custom: {{ watch_url }}",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        tpl = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.title == "Domain Alert")
        )
        assert tpl is not None
        assert tpl.content_config is not None
        assert tpl.content_config["default"]["include_domain"] is True
        assert tpl.content_config["default"]["body_template"] == "Custom: {{ watch_url }}"

    async def test_create_without_toggles_stores_null_content_config(
        self, client: AsyncClient, db_session
    ):
        """`_parse_content_config_from_form` returns None when no toggles or
        templates are set; NotificationTemplate.content_config stays null."""
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/nc-defaults/new",
            data={
                "title": "Plain Domain Alert",
                "apprise_url": "json://hooks.example.com/notify",
                "events": ["change_detected"],
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
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
            "/domains/example.com/nc-defaults/new",
            data={
                # title intentionally omitted — triggers validation error
                "apprise_url": "json://hooks.example.com/notify",
                "events": ["change_detected"],
                "content_config__include_significance": "1",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert re.search(
            r'name="content_config__include_significance"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_significance should persist through the error re-render"
