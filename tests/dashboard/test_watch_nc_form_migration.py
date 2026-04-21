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
        """The new form should not re-render the old `<details>Content Options</details>`."""
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        # Old partial's top-level summary text
        assert "Content Options" not in resp.text

    async def test_variable_chips_present(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(
            f"/watches/{watch.id}/notifications/add-row", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        assert "See all variables" in resp.text


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
