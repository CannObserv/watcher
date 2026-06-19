"""Integration tests for the domain detail NC-defaults section (#200).

Post-#200 the five legacy dispatch sources collapsed into one
``notification_templates`` table with an intrinsic ``visibility``. A domain's
notification defaults are now ``NotificationTemplate`` rows with
``visibility='domain'`` and ``domain_name`` set — there is no ``DomainNcRef``
junction and no assign-existing flow (a template has one intrinsic scope).

The ``GET /domains/{name}/nc-defaults`` partial renders two sections:
 - ``global_templates`` — read-only inherited globals (``visibility='global'``)
 - ``assigned`` — the domain's own templates (``visibility='domain'``)

CRUD on the domain section:
 - create:  POST /domains/{name}/notifications/new  → new visibility='domain' row
 - remove:  POST /domains/{name}/nc-defaults/remove/{template_id} → DELETEs the row

Removed routes (no longer tested — see the deletion notes in TestRemovedRoutes):
 - POST /domains/{name}/nc-defaults/add/{template_id}   (assign existing)
 - GET  /domains/{name}/nc-defaults/assign-row          (assign-existing picker)
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from ulid import ULID

from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    NotificationTemplate,
)

pytestmark = pytest.mark.integration

VALID_CHANNEL_ID = str(ULID())


async def _make_domain(db_session, name: str):
    from src.core.models import Domain

    domain = Domain(name=name)
    db_session.add(domain)
    await db_session.flush()
    return domain


async def _make_global_template(db_session, title: str, is_active: bool = True):
    """A library template (visibility='global') — inherited by every domain."""
    tpl = NotificationTemplate(
        title=title,
        remote_channel_id=str(ULID()),
        channel_hint="json",
        events=["change_detected"],
        visibility=VISIBILITY_GLOBAL,
        is_active=is_active,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


async def _make_domain_template(db_session, title: str, domain_name: str, is_active: bool = True):
    """A domain-scoped template (visibility='domain', domain_name set)."""
    tpl = NotificationTemplate(
        title=title,
        remote_channel_id=str(ULID()),
        channel_hint="json",
        events=["change_detected"],
        visibility=VISIBILITY_DOMAIN,
        domain_name=domain_name,
        is_active=is_active,
    )
    db_session.add(tpl)
    await db_session.flush()
    return tpl


class TestGlobalSection:
    """GET /domains/{name}/nc-defaults — the read-only inherited-globals section."""

    @pytest.mark.integration
    async def test_global_templates_appear_in_partial(self, client: AsyncClient, db_session):
        """Globals (visibility='global') render in the inherited section, unlinked."""
        await _make_domain(db_session, "global-show.example.com")
        await _make_global_template(db_session, "GlobalVisibleTemplate")

        resp = await client.get(
            "/domains/global-show.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"GlobalVisibleTemplate" in resp.content

    @pytest.mark.integration
    async def test_domain_template_not_in_global_section(self, client: AsyncClient, db_session):
        """A domain-scoped template is not a global, so it stays out of the Global section.

        It still renders in the Domain section — this asserts only that it is not
        promoted into the inherited-globals list (which queries visibility='global')."""
        await _make_domain(db_session, "non-global-check.example.com")
        await _make_global_template(db_session, "GlobalOne")
        await _make_domain_template(db_session, "DomainScopedOne", "non-global-check.example.com")

        resp = await client.get(
            "/domains/non-global-check.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        body = resp.text
        # The domain template appears once, inside the Domain section (id="domain-nc-tbody").
        domain_section = body.split('id="domain-nc-tbody"', 1)[1]
        assert "DomainScopedOne" in domain_section
        # And it does not appear in the global section (before the domain tbody marker).
        global_section = body.split('id="domain-nc-tbody"', 1)[0]
        assert "DomainScopedOne" not in global_section


class TestDomainSection:
    """The domain's own templates (visibility='domain') and their CRUD."""

    @pytest.mark.integration
    async def test_partial_loads(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "test-domain.com")
        resp = await client.get(
            "/domains/test-domain.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_partial_shows_domain_template_title(self, client: AsyncClient, db_session):
        """A visibility='domain' template for this domain renders in the partial."""
        await _make_domain(db_session, "show-assigned.com")
        await _make_domain_template(db_session, "MyDomainTemplate", "show-assigned.com")

        resp = await client.get(
            "/domains/show-assigned.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"MyDomainTemplate" in resp.content

    @pytest.mark.integration
    async def test_other_domains_templates_excluded(self, client: AsyncClient, db_session):
        """Domain section only shows templates whose domain_name matches."""
        await _make_domain(db_session, "mine.example.com")
        await _make_domain(db_session, "theirs.example.com")
        await _make_domain_template(db_session, "TheirTemplate", "theirs.example.com")

        resp = await client.get(
            "/domains/mine.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"TheirTemplate" not in resp.content

    @pytest.mark.integration
    async def test_remove_deletes_domain_template(self, client: AsyncClient, db_session):
        """POST .../nc-defaults/remove/{id} deletes the domain-scoped template row (#200)."""
        await _make_domain(db_session, "example.com")
        tpl = await _make_domain_template(db_session, "ToDelete", "example.com")
        tpl_id = tpl.id

        resp = await client.post(
            f"/domains/example.com/nc-defaults/remove/{tpl_id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        gone = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.id == tpl_id)
        )
        assert gone is None

    @pytest.mark.integration
    async def test_remove_nonexistent_returns_200(self, client: AsyncClient, db_session):
        """Removing an unknown template id is a no-op that still re-renders the partial."""
        await _make_domain(db_session, "remove-missing.com")

        resp = await client.post(
            f"/domains/remove-missing.com/nc-defaults/remove/{ULID()}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.integration
    async def test_remove_does_not_delete_global(self, client: AsyncClient, db_session):
        """A global template is not domain-scoped, so the remove route leaves it intact.

        The remove handler only deletes when visibility='domain' AND domain_name
        matches — a global passed here must survive."""
        await _make_domain(db_session, "guard.example.com")
        glob = await _make_global_template(db_session, "GuardedGlobal")
        glob_id = glob.id

        resp = await client.post(
            f"/domains/guard.example.com/nc-defaults/remove/{glob_id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        survivor = await db_session.scalar(
            select(NotificationTemplate).where(NotificationTemplate.id == glob_id)
        )
        assert survivor is not None
        assert survivor.visibility == VISIBILITY_GLOBAL


class TestCreateDomainTemplate:
    """POST /domains/{name}/notifications/new — create a visibility='domain' template."""

    @pytest.mark.integration
    async def test_create_makes_domain_scoped_template(self, client: AsyncClient, db_session):
        """Creating via the domain route makes a NotificationTemplate scoped to the domain.

        Post-#200 there is no separate DomainNcRef and no DOMAIN_NC_DEFAULT_ADDED
        audit — the row itself carries visibility='domain' + domain_name."""
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
        assert tpl.visibility == VISIBILITY_DOMAIN
        assert tpl.domain_name == "create-link.example.com"
        assert tpl.watched_item_id is None

    @pytest.mark.integration
    async def test_create_redirects_to_domain_page(self, client: AsyncClient, db_session):
        """After create, the response redirects back to the domain detail page."""
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
    async def test_created_template_appears_in_partial(self, client: AsyncClient, db_session):
        """A freshly created domain template renders in the domain nc-defaults partial."""
        await _make_domain(db_session, "create-render.example.com")

        await client.post(
            "/domains/create-render.example.com/notifications/new",
            data={
                "title": "RenderedDomainTemplate",
                "remote_channel_id": VALID_CHANNEL_ID,
                "channel_hint": "json",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )

        resp = await client.get(
            "/domains/create-render.example.com/nc-defaults",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert b"RenderedDomainTemplate" in resp.content

    @pytest.mark.integration
    async def test_create_requires_title(self, client: AsyncClient, db_session):
        """Missing title re-renders with an error (not a 500), creating no row."""
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

        count = await db_session.scalar(
            select(func.count())
            .select_from(NotificationTemplate)
            .where(NotificationTemplate.domain_name == "create-error.example.com")
        )
        assert count == 0

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


class TestRemovedRoutes:
    """Routes deleted by the #200 consolidation — assert they no longer exist.

    The assign-existing flow is gone: a template has one intrinsic visibility, so
    you can no longer attach an existing (global) template to a domain. Deleted:
      - POST /domains/{name}/nc-defaults/add/{template_id}
      - GET  /domains/{name}/nc-defaults/assign-row
    These previously created/queried DomainNcRef junction rows (also removed).
    """

    @pytest.mark.integration
    async def test_assign_existing_add_route_gone(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "gone-add.example.com")
        tpl = await _make_global_template(db_session, "OrphanGlobal")

        resp = await client.post(
            f"/domains/gone-add.example.com/nc-defaults/add/{tpl.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    async def test_assign_row_picker_route_gone(self, client: AsyncClient, db_session):
        await _make_domain(db_session, "gone-assign-row.example.com")

        resp = await client.get(
            "/domains/gone-assign-row.example.com/nc-defaults/assign-row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
