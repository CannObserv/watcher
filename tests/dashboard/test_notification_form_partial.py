"""Tests for the new notification_form partial — verifies the four-card
structure + preview pane render on template edit + add flows, and that
override add/remove HTMX routes work.
"""

import re

import pytest
from httpx import AsyncClient

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_template import NotificationTemplate

VALID_URL = "json://hooks.example.com/notify"


def _extract_preview_select_options(html: str) -> list[str]:
    """Return the list of `<option value="...">` values inside the
    `<select name="preview_event">` element, preserving order.
    """
    sel_match = re.search(
        r'<select[^>]*\bname="preview_event"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL,
    )
    if not sel_match:
        return []
    return re.findall(r'<option[^>]*\bvalue="([^"]+)"', sel_match.group(1))


def _extract_preview_selected(html: str) -> str | None:
    """Return the value of the selected `<option>` inside `select[name=preview_event]`."""
    sel_match = re.search(
        r'<select[^>]*\bname="preview_event"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL,
    )
    if not sel_match:
        return None
    sel_html = sel_match.group(1)
    m = re.search(r'<option[^>]*\bvalue="([^"]+)"[^>]*\bselected\b', sel_html)
    return m.group(1) if m else None


async def _make_template(db_session, title="T", **kwargs) -> NotificationTemplate:
    events = kwargs.pop("events", ["change_detected"])
    tpl = NotificationTemplate(
        title=title,
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=events,
        **kwargs,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


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


@pytest.mark.integration
class TestPreviewEventSelectorFiltering:
    """Issue #109 — preview event <select> only lists subscribed events.

    The preview pane's `<select name="preview_event">` must reflect the
    notification's actual subscription set so users can't preview an event
    the config would never fire on.
    """

    async def test_template_edit_preview_select_lists_only_subscribed_events(
        self, client: AsyncClient, db_session
    ):
        """Edit page: a template subscribed to 2 events shows only those 2 options."""
        tpl = await _make_template(
            db_session, "FilterMe", events=["change_detected", "watch_error"]
        )
        resp = await client.get(f"/notifications/{tpl.id}/edit")
        assert resp.status_code == 200
        options = _extract_preview_select_options(resp.text)
        assert options == ["change_detected", "watch_error"]

    async def test_template_edit_preview_select_defaults_selected_to_change_detected(
        self, client: AsyncClient, db_session
    ):
        """change_detected is selected by default when subscribed."""
        tpl = await _make_template(
            db_session, "DefaultSel", events=["change_detected", "watch_error"]
        )
        resp = await client.get(f"/notifications/{tpl.id}/edit")
        assert resp.status_code == 200
        assert _extract_preview_selected(resp.text) == "change_detected"

    async def test_template_edit_preview_selects_first_subscribed_when_change_detected_unsubscribed(
        self, client: AsyncClient, db_session
    ):
        """When change_detected is NOT subscribed, the first subscribed event is selected."""
        tpl = await _make_template(db_session, "NoCD", events=["watch_error", "watch_archived"])
        resp = await client.get(f"/notifications/{tpl.id}/edit")
        assert resp.status_code == 200
        options = _extract_preview_select_options(resp.text)
        assert options == ["watch_error", "watch_archived"]
        assert _extract_preview_selected(resp.text) == "watch_error"

    async def test_new_template_page_preview_select_falls_back_to_all_events(
        self, client: AsyncClient
    ):
        """GET /notifications/new (events=None) — selector lists all 8 events."""
        resp = await client.get("/notifications/new")
        assert resp.status_code == 200
        options = _extract_preview_select_options(resp.text)
        # Fallback: all known event_titles.keys() — at least change_detected present.
        assert "change_detected" in options
        # Sanity: more than one option (multiple event types).
        assert len(options) > 1
        assert _extract_preview_selected(resp.text) == "change_detected"
