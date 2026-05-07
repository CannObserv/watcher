"""Integration tests for domain detail NC defaults section.

Tests:
 - Global sub-table shows is_global_default=True templates
 - Domain sub-table shows DomainNcRef templates with CRUD
 - Inline create-and-link (POST /domains/{name}/nc-defaults/new)
 - Assign existing, remove, idempotency
"""

import pytest
from httpx import AsyncClient
from ulid import ULID

pytestmark = pytest.mark.integration

VALID_CHANNEL_ID = str(ULID())


async def _make_domain(db_session, name: str):
    from src.core.models import Domain

    domain = Domain(name=name)
    db_session.add(domain)
    await db_session.flush()
    return domain


async def _make_template(
    db_session,
    title: str,
    is_global_default: bool = False,
    is_active: bool = True,
):
    from src.core.models.notification_template import NotificationTemplate

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


class TestGlobalSubTable:
    """GET /domains/{name}/nc-defaults — global sub-table."""

    @pytest.mark.integration
    async def test_global_templates_appear_in_partial(self, client: AsyncClient, db_session):
        """Global (is_global_default=True) templates appear without any DomainNcRef."""
        await _make_domain(db_session, "global-show.example.com")
        await _make_template(db_session, "GlobalVisibleTemplate", is_global_default=True)

        resp = await client.get(
            "/domains/global-show.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"GlobalVisibleTemplate" in resp.content

    @pytest.mark.integration
    async def test_non_global_template_not_in_global_section(self, client: AsyncClient, db_session):
        """Non-global templates don't appear in the global sub-table."""
        await _make_domain(db_session, "non-global-check.example.com")
        await _make_template(db_session, "NotGlobalTemplate", is_global_default=False)

        resp = await client.get(
            "/domains/non-global-check.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"NotGlobalTemplate" not in resp.content


class TestDomainSubTable:
    """Domain NC CRUD via DomainNcRef."""

    @pytest.mark.integration
    async def test_add_and_remove(self, client: AsyncClient, db_session):
        from sqlalchemy import select

        from src.core.models.notification_template import DomainNcRef

        await _make_domain(db_session, "example.com")
        tpl = await _make_template(db_session, "D")

        resp = await client.post(
            f"/domains/example.com/nc-defaults/add/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        ref = await db_session.scalar(
            select(DomainNcRef).where(
                DomainNcRef.domain_name == "example.com",
                DomainNcRef.template_id == tpl.id,
            )
        )
        assert ref is not None

        resp = await client.post(
            f"/domains/example.com/nc-defaults/remove/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        ref = await db_session.scalar(
            select(DomainNcRef).where(
                DomainNcRef.domain_name == "example.com",
                DomainNcRef.template_id == tpl.id,
            )
        )
        assert ref is None

    @pytest.mark.integration
    async def test_partial_loads(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "test-domain.com")
        resp = await client.get(
            "/domains/test-domain.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_add_idempotent(self, client: AsyncClient, db_session):
        from sqlalchemy import func, select

        from src.core.models.notification_template import DomainNcRef

        await _make_domain(db_session, "idempotent-domain.com")
        tpl = await _make_template(db_session, "Idem")

        for _ in range(2):
            resp = await client.post(
                f"/domains/idempotent-domain.com/nc-defaults/add/{tpl.id}",
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200

        count = await db_session.scalar(
            select(func.count())
            .select_from(DomainNcRef)
            .where(
                DomainNcRef.domain_name == "idempotent-domain.com",
                DomainNcRef.template_id == tpl.id,
            )
        )
        assert count == 1

    @pytest.mark.integration
    async def test_remove_nonexistent_returns_200(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "remove-missing.com")
        tpl = await _make_template(db_session, "Missing")

        resp = await client.post(
            f"/domains/remove-missing.com/nc-defaults/remove/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_partial_shows_assigned_template_title(self, client: AsyncClient, db_session):
        from src.core.models.notification_template import DomainNcRef

        await _make_domain(db_session, "show-assigned.com")
        tpl = await _make_template(db_session, "MyAssignedTemplate")
        db_session.add(DomainNcRef(domain_name="show-assigned.com", template_id=tpl.id))
        await db_session.flush()

        resp = await client.get(
            "/domains/show-assigned.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"MyAssignedTemplate" in resp.content

    @pytest.mark.integration
    async def test_add_unknown_domain_returns_404(self, client: AsyncClient, db_session):
        tpl = await _make_template(db_session, "Orphan")
        resp = await client.post(
            f"/domains/no-such-domain.example.com/nc-defaults/add/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


class TestCreateAndLinkTemplate:
    """POST /domains/{name}/notifications/new — create template and auto-link via DomainNcRef."""

    @pytest.mark.integration
    async def test_create_new_template_links_to_domain(self, client: AsyncClient, db_session):
        """Creating a new template via domain route creates NotificationTemplate + DomainNcRef."""
        from sqlalchemy import select

        from src.core.models.notification_template import DomainNcRef, NotificationTemplate

        await _make_domain(db_session, "create-link.example.com")

        resp = await client.post(
            "/domains/create-link.example.com/notifications/new",
            data={
                "title": "NewDomainTemplate",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        tpl = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.title == "NewDomainTemplate")
        )
        assert tpl is not None
        assert tpl.is_global_default is False

        ref = await db_session.scalar(
            select(DomainNcRef).where(
                DomainNcRef.domain_name == "create-link.example.com",
                DomainNcRef.template_id == tpl.id,
            )
        )
        assert ref is not None

    @pytest.mark.integration
    async def test_create_new_template_returns_refreshed_partial(
        self, client: AsyncClient, db_session
    ):
        """Response after create redirects to the domain page."""
        await _make_domain(db_session, "create-refresh.example.com")

        resp = await client.post(
            "/domains/create-refresh.example.com/notifications/new",
            data={
                "title": "RefreshedTemplate",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "create-refresh.example.com" in resp.headers["location"]

    @pytest.mark.integration
    async def test_create_requires_title(self, client: AsyncClient, db_session):
        """Missing title returns an error response (not 500)."""
        await _make_domain(db_session, "create-error.example.com")

        resp = await client.post(
            "/domains/create-error.example.com/notifications/new",
            data={
                "title": "",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 422)

    @pytest.mark.integration
    async def test_create_unknown_domain_returns_404(self, client: AsyncClient, db_session):
        resp = await client.post(
            "/domains/no-such.example.com/notifications/new",
            data={
                "title": "ShouldFail",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestAssignRow:
    """GET /domains/{name}/nc-defaults/assign-row — picker excludes already-assigned."""

    @pytest.mark.integration
    async def test_assign_row_returns_200(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "assign-row-ok.example.com")
        resp = await client.get(
            "/domains/assign-row-ok.example.com/nc-defaults/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_assign_row_shows_unassigned(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "assign-row-show.example.com")
        await _make_template(db_session, "UnassignedForDomain")

        resp = await client.get(
            "/domains/assign-row-show.example.com/nc-defaults/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"UnassignedForDomain" in resp.content

    @pytest.mark.integration
    async def test_assign_row_excludes_global_templates(self, client: AsyncClient, db_session):
        """Global (is_global_default=True) templates must not appear in the domain picker.

        Assigning a global template as a domain default would cause double-dispatch
        (both the global and domain sources would fire for watches in that domain).
        """
        await _make_domain(db_session, "global-excl.example.com")
        await _make_template(db_session, "GlobalShouldBeHidden", is_global_default=True)

        resp = await client.get(
            "/domains/global-excl.example.com/nc-defaults/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"GlobalShouldBeHidden" not in resp.content
