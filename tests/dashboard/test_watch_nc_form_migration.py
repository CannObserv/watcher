"""Tests verifying the watch-NC add/edit forms render the new notification
form sections (Content card + Per-event overrides + Preview pane), Step 4.
"""

import re

import pytest
from httpx import AsyncClient

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.watch import Watch

VALID_URL = "json://hooks.example.com/notify"


async def _make_watch(db_session, url="https://example.com", **kwargs) -> Watch:
    watch = Watch(
        url=url,
        name=kwargs.pop("name", "Test Watch"),
        is_active=True,
        content_type="html",
        **kwargs,
    )
    db_session.add(watch)
    await db_session.flush()
    return watch


async def _make_nc(db_session, watch, **kwargs) -> WatchNotificationConfig:
    nc = WatchNotificationConfig(
        watch_id=watch.id,
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        is_active=True,
        **kwargs,
    )
    db_session.add(nc)
    await db_session.flush()
    return nc


@pytest.mark.integration
class TestWatchNcAddRowMigrated:
    async def test_content_card_present(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        # New Content card
        assert 'id="content-h-wnc-new-' in resp.text
        # New Overrides card
        assert 'id="overrides-h-wnc-new-' in resp.text
        # New Preview card
        assert 'id="preview-h-wnc-new-' in resp.text

    async def test_no_legacy_content_options_details(self, client: AsyncClient, db_session):
        """The old notification_content_options.html used a single
        <summary>Content Options</summary> top-level disclosure — ensure no
        accidental duplicate include leaves it behind."""
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        # Match the old partial's exact summary signature, discriminating from
        # any future use of the phrase "Content Options" in generic prose.
        assert not re.search(r"<summary[^>]*>\s*Content Options\s*</summary>", resp.text), (
            "legacy notification_content_options.html summary is still present"
        )

    async def test_variable_chips_present(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert "See all variables" in resp.text

    async def test_preview_pane_has_explicit_self_target(self, client: AsyncClient, db_session):
        """Regression: without hx-target='this' on the preview pane, HTMX
        inherits the outer form's hx-target="#watch-notifications" and the
        preview auto-load response wipes the notifications table."""
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        pane_tag = re.compile(
            rf'<div[^>]*id="nf-preview-pane-wnc-new-{watch.id}"[^>]*\bhx-target="this"',
            re.DOTALL,
        )
        assert pane_tag.search(resp.text), (
            "preview pane missing hx-target='this' on watch-NC add-row"
        )


@pytest.mark.integration
class TestWatchNcEditFormMigrated:
    async def test_content_card_present(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert f'id="content-h-wnc-{nc.id}"' in resp.text
        assert f'id="overrides-h-wnc-{nc.id}"' in resp.text
        assert f'id="preview-h-wnc-{nc.id}"' in resp.text

    async def test_existing_content_config_preserved_on_edit(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(
            db_session,
            watch,
            content_config={
                "default": {
                    "include_domain": True,
                    "body_template": "Custom: {{ watch_url }}",
                },
                "overrides": {},
            },
        )
        resp = await client.get(
            f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # include_domain must be rendered checked
        assert re.search(
            r'name="content_config__include_domain"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_domain checkbox not rendered as checked on edit"
        # body_template value must be in the textarea
        body_textarea = re.compile(
            r'<textarea[^>]*name="content_config__body_template"[^>]*>'
            r"[^<]*Custom: \{\{ watch_url \}\}"
        )
        assert body_textarea.search(resp.text), (
            "body_template value is not rendered in the textarea"
        )

    async def test_override_card_preserved_on_edit(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(
            db_session,
            watch,
            content_config={
                "default": {},
                "overrides": {"watch_error": {"include_significance": True}},
            },
        )
        resp = await client.get(
            f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert f"override-card-wnc-{nc.id}-watch_error" in resp.text


@pytest.mark.integration
class TestErrorPathPreservesContentConfig:
    """Routes must re-render the form with the user's content_config intact on
    validation errors — otherwise toggles/templates are silently dropped."""

    async def test_create_error_preserves_toggle_state(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={
                "apprise_url": "not-a-valid-scheme",
                "events": ["change_detected"],
                "content_config__include_domain": "1",
                "content_config__include_significance": "1",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Error message shown (confirms we're on the error-rerender path).
        # And content_config toggles survive the re-render.
        assert re.search(
            r'name="content_config__include_domain"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_domain should stay checked on error re-render"
        assert re.search(
            r'name="content_config__include_significance"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_significance should stay checked on error re-render"

    async def test_create_error_preserves_body_template(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={
                "apprise_url": "not-a-valid-scheme",
                "events": ["change_detected"],
                "content_config__body_template": "Custom: {{ watch_url }}",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        body_textarea = re.compile(
            r'<textarea[^>]*name="content_config__body_template"[^>]*>'
            r"[^<]*Custom: \{\{ watch_url \}\}"
        )
        assert body_textarea.search(resp.text), "body_template must survive error re-render"

    async def test_edit_error_preserves_toggle_state(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={
                "apprise_url": "not-a-valid-scheme",
                "events": ["change_detected"],
                "content_config__include_tags": "1",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert re.search(
            r'name="content_config__include_tags"[^>]*\bvalue="1"[^>]*\bchecked',
            resp.text,
        ), "include_tags should stay checked on edit error re-render"


@pytest.mark.integration
class TestWatchNcNewPage:
    async def test_new_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(f"/watches/{watch.id}/notifications/new")
        assert resp.status_code == 200
        assert b"plugin_schema" in resp.content
        assert b"watch_created" in resp.content  # disabled checkbox still present

    async def test_create_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={"apprise_url": VALID_URL, "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}" in resp.headers["location"]

    async def test_create_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={"apprise_url": "bad-url", "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"plugin_schema" in resp.content


@pytest.mark.integration
class TestWatchNcEditPage:
    async def test_edit_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.get(f"/watches/{watch.id}/notifications/{nc.id}/edit")
        assert resp.status_code == 200
        assert b"apprise_url" in resp.content

    async def test_edit_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={"apprise_url": VALID_URL, "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}" in resp.headers["location"]

    async def test_edit_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={"apprise_url": "bad-url", "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"apprise_url" in resp.content
