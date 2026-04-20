"""Tests for the new notification_form partial — verifies the four-card
structure + preview pane render on template edit + add flows, and that
override add/remove HTMX routes work.
"""

import pytest
from httpx import AsyncClient

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_template import NotificationTemplate

VALID_URL = "json://hooks.example.com/notify"


async def _make_template(db_session, title="T", **kwargs) -> NotificationTemplate:
    tpl = NotificationTemplate(
        title=title,
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        **kwargs,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


@pytest.mark.integration
class TestTemplateAddRowShape:
    async def test_has_four_cards(self, client: AsyncClient):
        resp = await client.get("/notifications/add-row", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        text = resp.text
        # Section headings present
        for heading in ("Content", "Per-event overrides", "Preview"):
            assert heading in text, f"missing section heading: {heading}"

    async def test_preview_pane_present(self, client: AsyncClient):
        resp = await client.get("/notifications/add-row", headers={"HX-Request": "true"})
        assert "nf-preview-pane-tpl-new" in resp.text

    async def test_add_override_button_present(self, client: AsyncClient):
        resp = await client.get("/notifications/add-row", headers={"HX-Request": "true"})
        assert "+ Add override" in resp.text

    async def test_variable_chips_present(self, client: AsyncClient):
        resp = await client.get("/notifications/add-row", headers={"HX-Request": "true"})
        # Primary chip row includes watch_name etc.
        assert "watch_name" in resp.text
        assert "See all variables" in resp.text


@pytest.mark.integration
class TestTemplateEditFormShape:
    async def test_edit_form_uses_new_partial(self, client: AsyncClient, db_session):
        tpl = await _make_template(db_session)
        resp = await client.get(
            f"/notifications/{tpl.id}/edit-form", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        text = resp.text
        for heading in ("Basics", "Subscribe", "Content", "Per-event overrides", "Preview"):
            assert heading in text, f"missing: {heading}"

    async def test_edit_form_includes_preview_pane(self, client: AsyncClient, db_session):
        tpl = await _make_template(db_session)
        resp = await client.get(
            f"/notifications/{tpl.id}/edit-form", headers={"HX-Request": "true"}
        )
        assert f"nf-preview-pane-tpl-{tpl.id}" in resp.text

    async def test_edit_form_preserves_existing_content_config(
        self, client: AsyncClient, db_session
    ):
        tpl = await _make_template(
            db_session,
            content_config={
                "default": {
                    "include_domain": True,
                    "title_template": "My Custom: {{ watch_name }}",
                },
                "overrides": {},
            },
        )
        resp = await client.get(
            f"/notifications/{tpl.id}/edit-form", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        # Domain checkbox should be checked
        assert 'name="content_config__include_domain" value="1"' in resp.text
        # Title template should be in the textarea
        assert "My Custom:" in resp.text

    async def test_edit_form_renders_existing_override_card(self, client: AsyncClient, db_session):
        tpl = await _make_template(
            db_session,
            content_config={
                "default": {},
                "overrides": {"watch_error": {"include_significance": True}},
            },
        )
        resp = await client.get(
            f"/notifications/{tpl.id}/edit-form", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        # Override card for watch_error should be rendered
        assert f"override-card-tpl-{tpl.id}-watch_error" in resp.text


@pytest.mark.integration
class TestOverrideAddPicker:
    async def test_returns_picker_with_subscribed_events(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/add-picker",
            params=[
                ("form_id", "tpl-new"),
                ("events", "change_detected"),
                ("events", "watch_error"),
            ],
        )
        assert resp.status_code == 200
        assert 'id="override-picker-tpl-new"' in resp.text
        assert "change_detected" in resp.text
        assert "watch_error" in resp.text

    async def test_excludes_already_overridden_events(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/add-picker",
            params=[
                ("form_id", "tpl-new"),
                ("events", "change_detected"),
                ("events", "watch_error"),
                ("content_config__override__change_detected__include_domain", "1"),
            ],
        )
        assert resp.status_code == 200
        # watch_error should still be pickable, change_detected should not
        assert 'value="watch_error"' in resp.text
        assert 'value="change_detected"' not in resp.text

    async def test_empty_state_when_nothing_pickable(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/add-picker",
            params={"form_id": "tpl-new"},
        )
        assert resp.status_code == 200
        assert "Subscribe to events first" in resp.text


@pytest.mark.integration
class TestOverrideCardRoute:
    async def test_returns_card_for_valid_event(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/card",
            params={"form_id": "tpl-new", "event_type": "change_detected"},
        )
        assert resp.status_code == 200
        assert "override-card-tpl-new-change_detected" in resp.text
        assert "× Remove" in resp.text

    async def test_seeds_card_from_current_default_state(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/card",
            params={
                "form_id": "tpl-new",
                "event_type": "change_detected",
                "content_config__include_domain": "1",
            },
        )
        assert resp.status_code == 200
        # The override card's domain checkbox should be checked (seeded from default)
        assert (
            'name="content_config__override__change_detected__include_domain" value="1"'
            in resp.text
        )

    async def test_rejects_invalid_event_type(self, client: AsyncClient):
        resp = await client.get(
            "/notifications/overrides/card",
            params={"form_id": "tpl-new", "event_type": "not_a_real_event"},
        )
        assert resp.status_code == 400
