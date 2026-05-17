"""Integration tests for watch detail NC section — unified notifications table.

Tests assign-from-library, unassign, copy-to-local, copy-local, and the unified
table that shows Global / Domain / Watch-assigned / Local rows with a Source column.
"""

import pytest
from ulid import ULID

from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.watch import ContentType, Watch
from tests.conftest import make_watch

pytestmark = pytest.mark.integration


async def _make_watch(db_session, name: str = "W", domain: str | None = None) -> Watch:
    # Use name-based URL to avoid uniqueness constraint when multiple watches are created.
    slug = name.lower().replace(" ", "-")
    return await make_watch(
        db_session,
        name=name,
        primary_url=f"https://example-{slug}.com",
        content_type=ContentType.HTML,
        effective_domain=domain,
    )


async def _make_template(
    db_session,
    title: str = "T",
    is_global_default: bool = False,
    is_active: bool = True,
) -> NotificationTemplate:
    tpl = NotificationTemplate(
        title=title,
        remote_channel_id=str(ULID()),
        channel_hint="json",
        events=["change_detected"],
        is_global_default=is_global_default,
        is_active=is_active,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


class TestUnifiedNotificationsTable:
    """GET /partials/watch-notifications/{watch_id} — unified table with Source column."""

    @pytest.mark.integration
    async def test_global_template_appears_without_watch_nc_ref(self, client, db_session):
        """Global templates show in the table even with no WatchNcRef row."""
        watch = await _make_watch(db_session, "Global Watch")
        await _make_template(db_session, "GlobalTemplate", is_global_default=True)

        resp = await client.get(
            f"/partials/watch-notifications/{watch.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"GlobalTemplate" in resp.content

    @pytest.mark.integration
    async def test_domain_template_appears_for_matching_domain(self, client, db_session):
        """Domain templates appear for watches whose effective_domain matches."""
        from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain

        domain_name = "watch-nc-test.example.com"
        db_session.add(
            Domain(
                name=domain_name,
                min_interval=DEFAULT_MIN_INTERVAL,
                max_concurrency=DEFAULT_MAX_CONCURRENCY,
                current_interval=DEFAULT_MIN_INTERVAL,
            )
        )
        watch = await _make_watch(db_session, "Domain Watch", domain=domain_name)
        tpl = await _make_template(db_session, "DomainTemplate")
        db_session.add(DomainNcRef(domain_name=domain_name, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/partials/watch-notifications/{watch.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"DomainTemplate" in resp.content

    @pytest.mark.integration
    async def test_domain_template_does_not_appear_for_other_domain(self, client, db_session):
        """Domain templates do not appear for watches in a different domain."""
        from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain

        domain_name = "other-domain.example.com"
        db_session.add(
            Domain(
                name=domain_name,
                min_interval=DEFAULT_MIN_INTERVAL,
                max_concurrency=DEFAULT_MAX_CONCURRENCY,
                current_interval=DEFAULT_MIN_INTERVAL,
            )
        )
        watch = await _make_watch(db_session, "Wrong Domain Watch", domain="different.example.com")
        tpl = await _make_template(db_session, "OtherDomainTemplate")
        db_session.add(DomainNcRef(domain_name=domain_name, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/partials/watch-notifications/{watch.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"OtherDomainTemplate" not in resp.content

    @pytest.mark.integration
    async def test_watch_assigned_template_appears(self, client, db_session):
        """Manually-assigned (WatchNcRef) templates appear in the table."""
        watch = await _make_watch(db_session, "Assigned Watch")
        tpl = await _make_template(db_session, "AssignedTemplate")
        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/partials/watch-notifications/{watch.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"AssignedTemplate" in resp.content

    @pytest.mark.integration
    async def test_global_template_not_duplicated_when_also_in_watch_nc_ref(
        self, client, db_session
    ):
        """A global template also in WatchNcRef appears only as a global row, not also as
        a watch-assigned row."""
        watch = await _make_watch(db_session, "Dedup Watch")
        tpl = await _make_template(db_session, "DedupTemplate", is_global_default=True)
        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/partials/watch-notifications/{watch.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        content = resp.text
        # Global row present
        assert f'id="tpl-global-{tpl.id}"' in content
        # Watch-assigned row absent (deduplicated)
        assert f'id="tpl-ref-{tpl.id}"' not in content


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
            .where(WatchNcRef.watch_id == watch.id, WatchNcRef.template_id == tpl.id)
        )
        assert count_result.scalar() == 1

    @pytest.mark.integration
    async def test_assign_returns_template_title(self, client, db_session):
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
            remote_channel_id=str(ULID()),
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
            remote_channel_id=str(ULID()),
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
        watch1 = await _make_watch(db_session, "Watch A")
        watch2 = await _make_watch(db_session, "Watch B")
        nc = WatchNotificationConfig(
            watch_id=watch1.id,
            title="Belongs to A",
            remote_channel_id=str(ULID()),
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
    """GET /watches/{watch_id}/notifications/assign-row — picker excludes global/domain."""

    @pytest.mark.integration
    async def test_returns_200(self, client, db_session):
        watch = await _make_watch(db_session, "Assign Row Watch")
        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_shows_non_global_non_domain_templates(self, client, db_session):
        """Plain (non-global, non-domain) templates appear in the assign picker."""
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

        db_session.add(WatchNcRef(watch_id=watch.id, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"HiddenTemplate" not in resp.content

    @pytest.mark.integration
    async def test_hides_global_templates_from_picker(self, client, db_session):
        """Global templates are not shown in the assign picker — they auto-dispatch."""
        watch = await _make_watch(db_session, "Global Picker Watch")
        await _make_template(db_session, "GlobalPickerTemplate", is_global_default=True)

        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"GlobalPickerTemplate" not in resp.content

    @pytest.mark.integration
    async def test_hides_domain_templates_from_picker(self, client, db_session):
        """Domain templates for this watch's domain are not shown in the assign picker."""
        from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain

        domain_name = "assign-row-domain.example.com"
        db_session.add(
            Domain(
                name=domain_name,
                min_interval=DEFAULT_MIN_INTERVAL,
                max_concurrency=DEFAULT_MAX_CONCURRENCY,
                current_interval=DEFAULT_MIN_INTERVAL,
            )
        )
        watch = await _make_watch(db_session, "Domain Picker Watch", domain=domain_name)
        tpl = await _make_template(db_session, "DomainPickerTemplate")
        db_session.add(DomainNcRef(domain_name=domain_name, template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            f"/watches/{watch.id}/notifications/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"DomainPickerTemplate" not in resp.content
