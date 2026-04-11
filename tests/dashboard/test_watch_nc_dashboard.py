"""Integration tests for watch detail NC section — Library/Local groups.

Tests assign-from-library, unassign, copy-to-local, and copy-local actions.
"""

import pytest

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import NotificationTemplate, WatchNcRef
from src.core.models.watch import ContentType, Watch

pytestmark = pytest.mark.integration

VALID_URL = "json://hooks.example.com/notify"


async def _make_watch(db_session, name: str = "W") -> Watch:
    watch = Watch(name=name, url="https://example.com", content_type=ContentType.HTML)
    db_session.add(watch)
    await db_session.flush()
    return watch


async def _make_template(db_session, title: str = "T") -> NotificationTemplate:
    tpl = NotificationTemplate(
        title=title,
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


class TestAssignTemplateToWatch:
    """POST /watches/{watch_id}/notifications/assign/{template_id}"""

    @pytest.mark.integration
    async def test_assign_creates_ref(self, client, db_session):
        from sqlalchemy import select

        watch = await _make_watch(db_session, "Assign Watch")
        tpl = await _make_template(db_session, "Assign T")

        resp = await client.post(
            f"/watches/{watch.id}/notifications/assign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        result = await db_session.execute(
            select(WatchNcRef).where(
                WatchNcRef.watch_id == watch.id,
                WatchNcRef.template_id == tpl.id,
            )
        )
        assert result.scalar_one_or_none() is not None

    @pytest.mark.integration
    async def test_assign_idempotent(self, client, db_session):
        """Assigning twice does not create a duplicate ref."""
        from sqlalchemy import func, select

        watch = await _make_watch(db_session, "Idempotent Watch")
        tpl = await _make_template(db_session, "Idempotent T")

        await client.post(
            f"/watches/{watch.id}/notifications/assign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        resp = await client.post(
            f"/watches/{watch.id}/notifications/assign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        count_result = await db_session.execute(
            select(func.count())
            .select_from(WatchNcRef)
            .where(
                WatchNcRef.watch_id == watch.id,
                WatchNcRef.template_id == tpl.id,
            )
        )
        assert count_result.scalar() == 1

    @pytest.mark.integration
    async def test_assign_returns_library_section(self, client, db_session):
        """Response includes the assigned template title in the library section."""
        watch = await _make_watch(db_session, "Library Watch")
        tpl = await _make_template(db_session, "LibraryTemplate")

        resp = await client.post(
            f"/watches/{watch.id}/notifications/assign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"LibraryTemplate" in resp.content


class TestUnassignTemplate:
    """POST /watches/{watch_id}/notifications/unassign/{template_id}"""

    @pytest.mark.integration
    async def test_unassign_removes_ref(self, client, db_session):
        from sqlalchemy import select

        watch = await _make_watch(db_session, "Unassign Watch")
        tpl = await _make_template(db_session, "Unassign T")

        # Assign first
        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.post(
            f"/watches/{watch.id}/notifications/unassign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        result = await db_session.execute(
            select(WatchNcRef).where(
                WatchNcRef.watch_id == watch.id,
                WatchNcRef.template_id == tpl.id,
            )
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.integration
    async def test_unassign_no_ref_still_200(self, client, db_session):
        """Unassigning a template that is not assigned returns 200 gracefully."""
        watch = await _make_watch(db_session, "No Ref Watch")
        tpl = await _make_template(db_session, "No Ref T")

        resp = await client.post(
            f"/watches/{watch.id}/notifications/unassign/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestCopyTemplateToLocal:
    """POST /watches/{watch_id}/notifications/copy-template/{template_id}"""

    @pytest.mark.integration
    async def test_copy_creates_local_config(self, client, db_session):
        from sqlalchemy import select

        watch = await _make_watch(db_session, "Copy Watch")
        tpl = await _make_template(db_session, "Copy T")

        # Assign first
        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.post(
            f"/watches/{watch.id}/notifications/copy-template/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        local = await db_session.scalar(
            select(WatchNotificationConfig).where(WatchNotificationConfig.watch_id == watch.id)
        )
        assert local is not None
        assert local.channel_hint == "json"

    @pytest.mark.integration
    async def test_copy_removes_ref(self, client, db_session):
        from sqlalchemy import select

        watch = await _make_watch(db_session, "Copy Ref Watch")
        tpl = await _make_template(db_session, "Copy Ref T")

        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        await client.post(
            f"/watches/{watch.id}/notifications/copy-template/{tpl.id}",
            headers={"HX-Request": "true"},
        )

        ref = await db_session.scalar(select(WatchNcRef).where(WatchNcRef.watch_id == watch.id))
        assert ref is None

    @pytest.mark.integration
    async def test_copy_404_for_unknown_template(self, client, db_session):
        from ulid import ULID

        watch = await _make_watch(db_session, "404 Watch")
        fake_id = str(ULID())

        resp = await client.post(
            f"/watches/{watch.id}/notifications/copy-template/{fake_id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


class TestCopyLocalConfig:
    """POST /watches/{watch_id}/notifications/{config_id}/copy"""

    @pytest.mark.integration
    async def test_copy_duplicates_config(self, client, db_session):
        from sqlalchemy import func, select

        watch = await _make_watch(db_session, "Local Copy Watch")
        nc = WatchNotificationConfig(
            watch_id=watch.id,
            title="Original",
            apprise_url=encrypt_apprise_url(VALID_URL),
            channel_hint="json",
            events=["change_detected"],
        )
        db_session.add(nc)
        await db_session.flush()

        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/copy",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        count = await db_session.scalar(
            select(func.count())
            .select_from(WatchNotificationConfig)
            .where(WatchNotificationConfig.watch_id == watch.id)
        )
        assert count == 2

    @pytest.mark.integration
    async def test_copy_appends_copy_suffix(self, client, db_session):
        from sqlalchemy import select

        watch = await _make_watch(db_session, "Suffix Watch")
        nc = WatchNotificationConfig(
            watch_id=watch.id,
            title="My Config",
            apprise_url=encrypt_apprise_url(VALID_URL),
            channel_hint="json",
            events=["change_detected"],
        )
        db_session.add(nc)
        await db_session.flush()

        await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/copy",
            headers={"HX-Request": "true"},
        )

        results = await db_session.execute(
            select(WatchNotificationConfig).where(
                WatchNotificationConfig.watch_id == watch.id,
                WatchNotificationConfig.title == "My Config (copy)",
            )
        )
        assert results.scalar_one_or_none() is not None

    @pytest.mark.integration
    async def test_copy_404_for_wrong_watch(self, client, db_session):
        """Cannot copy a config that belongs to a different watch."""
        watch1 = await _make_watch(db_session, "Watch A")
        watch2 = await _make_watch(db_session, "Watch B")
        nc = WatchNotificationConfig(
            watch_id=watch1.id,
            title="Belongs to A",
            apprise_url=encrypt_apprise_url(VALID_URL),
            channel_hint="json",
            events=["change_detected"],
        )
        db_session.add(nc)
        await db_session.flush()

        resp = await client.post(
            f"/watches/{watch2.id}/notifications/{nc.id}/copy",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


class TestAssignRow:
    """GET /watches/{watch_id}/notifications/assign-row"""

    @pytest.mark.integration
    async def test_returns_200(self, client, db_session):
        watch = await _make_watch(db_session, "Assign Row Watch")
        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_shows_unassigned_templates(self, client, db_session):
        watch = await _make_watch(db_session, "Picker Watch")
        await _make_template(db_session, "PickerTemplate")

        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"PickerTemplate" in resp.content

    @pytest.mark.integration
    async def test_hides_already_assigned_templates(self, client, db_session):
        watch = await _make_watch(db_session, "Hide Watch")
        tpl = await _make_template(db_session, "HiddenTemplate")

        # Assign it
        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"HiddenTemplate" not in resp.content
