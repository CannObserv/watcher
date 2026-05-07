"""Tests verifying the watch-NC add/edit forms render the new notification
form sections (Content card + Per-event overrides + Preview pane), Step 4.

After Phase 5 (#137), forms post `remote_channel_id` rather than an
Apprise URL.
"""

import re

import pytest
from httpx import AsyncClient
from ulid import ULID

from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.watch import Watch
from tests.conftest import make_watch

VALID_CHANNEL_ID = str(ULID())


async def _make_watch(db_session, url="https://example.com", **kwargs) -> Watch:
    return await make_watch(
        db_session,
        url=url,
        name=kwargs.pop("name", "Test Watch"),
        is_active=True,
        content_type="html",
        **kwargs,
    )


async def _make_nc(db_session, watch, **kwargs) -> WatchNotificationConfig:
    nc = WatchNotificationConfig(
        watch_id=watch.id,
        channel_hint="json",
        events=["change_detected"],
        is_active=True,
        remote_channel_id=str(ULID()),
        **kwargs,
    )
    db_session.add(nc)
    await db_session.flush()
    return nc


@pytest.mark.integration
class TestErrorPathPreservesContentConfig:
    """Routes must re-render the form with the user's content_config intact on
    validation errors — otherwise toggles/templates are silently dropped."""

    async def test_create_error_preserves_toggle_state(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        # Missing remote_channel_id triggers the validation error path.
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={
                "events": ["change_detected"],
                "content_config__include_domain": "1",
                "content_config__include_significance": "1",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
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

    async def test_edit_error_preserves_events(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={
                "events": ["change_detected", "watch_error"],
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert re.search(
            r'name="events"[^>]*value="watch_error"[^>]*checked',
            resp.text,
        ), "watch_error should stay checked on edit error re-render"


@pytest.mark.integration
class TestWatchNcNewPage:
    async def test_new_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(f"/watches/{watch.id}/notifications/new")
        assert resp.status_code == 200
        assert b"remote_channel_id" in resp.content
        assert b"watch_created" in resp.content  # disabled checkbox still present

    async def test_create_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}" in resp.headers["location"]

    async def test_create_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={"events": ["change_detected"]},  # missing remote_channel_id
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"remote_channel_id" in resp.content


@pytest.mark.integration
class TestWatchNcEditPage:
    async def test_edit_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.get(f"/watches/{watch.id}/notifications/{nc.id}/edit")
        assert resp.status_code == 200
        assert b"remote_channel_id" in resp.content

    async def test_edit_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}#watch-notifications" in resp.headers["location"]

    async def test_edit_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={"events": ["change_detected"]},  # missing remote_channel_id
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"remote_channel_id" in resp.content
