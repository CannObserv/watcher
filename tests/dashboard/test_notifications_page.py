"""Integration tests for the /notifications dashboard page (template library CRUD UI)."""

import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_template import NotificationTemplate, WatchNcRef
from src.core.notifications.dispatcher import DispatchResult

VALID_URL = "json://hooks.example.com/notify"


async def _make_template(db_session, title: str = "T", **kwargs) -> NotificationTemplate:
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
async def test_notifications_page_loads(client: AsyncClient):
    resp = await client.get("/notifications")
    assert resp.status_code == 200
    assert b"Notification" in resp.content


@pytest.mark.integration
async def test_notification_new_page_loads(client: AsyncClient):
    resp = await client.get("/notifications/new")
    assert resp.status_code == 200
    assert b"New Notification Template" in resp.content
    assert b"plugin_schema" in resp.content  # channel picker present


@pytest.mark.integration
async def test_notification_new_page_has_links_section_with_watch_url_toggle(
    client: AsyncClient,
):
    """The Content card's Links section exposes Watch URL + Change URL toggles."""
    resp = await client.get("/notifications/new")
    assert resp.status_code == 200
    body = resp.content.decode()
    # Section heading renamed Link -> Links
    assert ">Links<" in body
    # Both toggle inputs present (asserts the actual checkbox, not just text).
    assert 'name="content_config__include_watch_url"' in body
    assert 'name="content_config__include_change_dashboard_url"' in body
    # Legacy label gone
    assert "Dashboard link (change URL)" not in body


@pytest.mark.integration
async def test_create_template_redirects_on_success(client: AsyncClient, db_session):
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Ops Alert",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"


@pytest.mark.integration
async def test_create_template_rerenders_page_on_title_error(client: AsyncClient):
    resp = await client.post(
        "/notifications/new",
        data={"title": "", "apprise_url": VALID_URL, "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Title is required" in resp.content
    assert b"New Notification Template" in resp.content


@pytest.mark.integration
async def test_create_template_invalid_url_returns_form_with_error(client: AsyncClient):
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Bad",
            "apprise_url": "not-a-valid-apprise-url",
            "events": ["change_detected"],
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # Returns the add-row form again with an error message
    assert b"error" in resp.content.lower() or b"invalid" in resp.content.lower()


@pytest.mark.integration
async def test_edit_saves_title_and_redirects(client: AsyncClient, db_session):
    """POST /{id}/edit with valid data updates the template and redirects to /notifications."""
    tpl = await _make_template(db_session, "OldTitle")

    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={
            "title": "NewTitle",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"

    await db_session.refresh(tpl)
    assert tpl.title == "NewTitle"


@pytest.mark.integration
async def test_edit_invalid_url_returns_page_with_error(client: AsyncClient, db_session):
    """POST /{id}/edit with an invalid Apprise URL rerenders the edit page with an error."""
    tpl = await _make_template(db_session, "ValidTitle")

    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={
            "title": "ValidTitle",
            "apprise_url": "not-valid",
            "events": ["change_detected"],
        },
    )
    assert resp.status_code == 200
    assert b"error" in resp.content.lower() or b"invalid" in resp.content.lower()
    assert b"Edit" in resp.content


@pytest.mark.integration
async def test_edit_error_preserves_events(client: AsyncClient, db_session):
    """POST /{id}/edit with invalid URL rerenders edit page with submitted events intact."""
    # Template stored with only change_detected; submit watch_error as well
    tpl = await _make_template(db_session, "EventTest")

    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={
            "title": "EventTest",
            "apprise_url": "not-valid",
            "events": ["change_detected", "watch_error"],
        },
    )
    assert resp.status_code == 200
    assert re.search(
        r'name="events"[^>]*value="watch_error"[^>]*checked',
        resp.text,
    ), "watch_error should stay checked on edit error re-render"


@pytest.mark.integration
async def test_toggle_flips_is_active(client: AsyncClient, db_session):
    """POST /{id}/toggle flips is_active and returns the refreshed list."""
    tpl = await _make_template(db_session, "ToggleMe")
    assert tpl.is_active is True

    resp = await client.post(f"/notifications/{tpl.id}/toggle", headers={"HX-Request": "true"})
    assert resp.status_code == 200

    await db_session.refresh(tpl)
    assert tpl.is_active is False


@pytest.mark.integration
async def test_toggle_inactive_back_to_active(client: AsyncClient, db_session):
    """Two toggles restores is_active to True."""
    tpl = await _make_template(db_session, "ToggleTwice")

    await client.post(f"/notifications/{tpl.id}/toggle", headers={"HX-Request": "true"})
    await client.post(f"/notifications/{tpl.id}/toggle", headers={"HX-Request": "true"})

    await db_session.refresh(tpl)
    assert tpl.is_active is True


@pytest.mark.integration
async def test_delete_succeeds_when_no_refs(client: AsyncClient, db_session):
    """DELETE /{id}/delete removes an unreferenced template and returns the list."""
    tpl = await _make_template(db_session, "DeleteMe")
    tpl_id = str(tpl.id)

    resp = await client.delete(f"/notifications/{tpl_id}/delete", headers={"HX-Request": "true"})
    assert resp.status_code == 200

    result = await db_session.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == tpl.id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.integration
async def test_delete_blocked_when_watch_ref_exists(client: AsyncClient, db_session):
    """DELETE /{id}/delete returns 409 when a WatchNcRef still references the template."""
    from src.core.models.watch import ContentType, Watch

    watch = Watch(name="W", url="https://example.com", content_type=ContentType.HTML)
    db_session.add(watch)
    tpl = await _make_template(db_session, "Referenced")
    db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
    await db_session.flush()

    resp = await client.delete(f"/notifications/{tpl.id}/delete", headers={"HX-Request": "true"})
    assert resp.status_code == 409


@pytest.mark.integration
async def test_test_result_returns_flash_on_success(client: AsyncClient, db_session):
    """POST /{id}/test-result dispatches and returns an OOB flash partial."""
    tpl = await _make_template(db_session, "TestMe")

    dispatch_result = DispatchResult(success=True, reason="sent")
    with patch(
        "src.dashboard.routes.dispatch_event",
        new_callable=AsyncMock,
        return_value=dispatch_result,
    ):
        resp = await client.post(
            f"/notifications/{tpl.id}/test-result", headers={"HX-Request": "true"}
        )

    assert resp.status_code == 200
    assert b"flash" in resp.content.lower() or b"sent" in resp.content.lower()


@pytest.mark.integration
async def test_test_result_returns_flash_on_dispatch_failure(client: AsyncClient, db_session):
    """POST /{id}/test-result handles dispatch exceptions without 5xx."""
    tpl = await _make_template(db_session, "FailMe")

    with patch(
        "src.dashboard.routes.dispatch_event",
        new_callable=AsyncMock,
        side_effect=Exception("boom"),
    ):
        resp = await client.post(
            f"/notifications/{tpl.id}/test-result", headers={"HX-Request": "true"}
        )

    assert resp.status_code == 200


@pytest.mark.integration
async def test_duplicate_creates_copy(client: AsyncClient, db_session):
    """POST /{id}/duplicate creates a new template titled '<title> (copy)'."""
    tpl = await _make_template(db_session, "Original")

    resp = await client.post(f"/notifications/{tpl.id}/duplicate", headers={"HX-Request": "true"})
    assert resp.status_code == 200

    result = await db_session.execute(
        select(NotificationTemplate).where(NotificationTemplate.title == "Original (copy)")
    )
    copy = result.scalar_one_or_none()
    assert copy is not None
    assert copy.channel_hint == tpl.channel_hint
    assert copy.events == tpl.events
    assert copy.is_global_default is False


@pytest.mark.integration
async def test_duplicate_404_for_unknown_id(client: AsyncClient):
    """POST /{id}/duplicate returns 404 for a non-existent template."""
    from ulid import ULID

    resp = await client.post(f"/notifications/{ULID()}/duplicate", headers={"HX-Request": "true"})
    assert resp.status_code == 404


@pytest.mark.integration
async def test_notification_edit_page_loads(client: AsyncClient, db_session):
    tpl = await _make_template(db_session, title="Edit Me")
    await db_session.commit()
    resp = await client.get(f"/notifications/{tpl.id}/edit")
    assert resp.status_code == 200
    assert b"Edit Me" in resp.content
    assert b"apprise_url" in resp.content


@pytest.mark.integration
async def test_edit_template_redirects_on_success(client: AsyncClient, db_session):
    tpl = await _make_template(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={"title": "Updated", "apprise_url": VALID_URL, "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"


@pytest.mark.integration
async def test_edit_template_rerenders_page_on_error(client: AsyncClient, db_session):
    tpl = await _make_template(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={"title": "X", "apprise_url": "not-a-valid-url", "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Edit" in resp.content
