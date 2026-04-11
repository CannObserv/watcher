"""Integration tests for domain detail NC defaults section."""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

VALID_URL = "json://hooks.example.com/notify"


async def test_add_and_remove_domain_nc_default(client: AsyncClient, db_session):
    from sqlalchemy import select

    from src.core.crypto import encrypt_apprise_url
    from src.core.models import Domain
    from src.core.models.notification_template import DomainNcRef, NotificationTemplate

    domain = Domain(name="example.com")
    db_session.add(domain)
    tpl = NotificationTemplate(
        title="D",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()

    # Add domain default
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

    # Remove domain default
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


async def test_domain_nc_defaults_partial_loads(client: AsyncClient, db_session):
    from src.core.models import Domain

    domain = Domain(name="test-domain.com")
    db_session.add(domain)
    await db_session.flush()

    resp = await client.get(
        "/domains/test-domain.com/nc-defaults",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


async def test_add_idempotent(client: AsyncClient, db_session):
    """Adding the same template twice does not create a duplicate DomainNcRef."""
    from sqlalchemy import func, select

    from src.core.crypto import encrypt_apprise_url
    from src.core.models import Domain
    from src.core.models.notification_template import DomainNcRef, NotificationTemplate

    domain = Domain(name="idempotent-domain.com")
    db_session.add(domain)
    tpl = NotificationTemplate(
        title="Idem",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()

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


async def test_remove_nonexistent_returns_200(client: AsyncClient, db_session):
    """Removing a template not assigned to domain returns 200 gracefully."""
    from src.core.crypto import encrypt_apprise_url
    from src.core.models import Domain
    from src.core.models.notification_template import NotificationTemplate

    domain = Domain(name="remove-missing.com")
    db_session.add(domain)
    tpl = NotificationTemplate(
        title="Missing",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()

    resp = await client.post(
        f"/domains/remove-missing.com/nc-defaults/remove/{tpl.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


async def test_partial_shows_assigned_template_title(client: AsyncClient, db_session):
    """Partial lists assigned template by title."""
    from src.core.crypto import encrypt_apprise_url
    from src.core.models import Domain
    from src.core.models.notification_template import DomainNcRef, NotificationTemplate

    domain = Domain(name="show-assigned.com")
    db_session.add(domain)
    tpl = NotificationTemplate(
        title="MyAssignedTemplate",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
    )
    db_session.add(tpl)
    await db_session.flush()
    db_session.add(DomainNcRef(domain_name="show-assigned.com", template_id=tpl.id))
    await db_session.flush()

    resp = await client.get(
        "/domains/show-assigned.com/nc-defaults",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert b"MyAssignedTemplate" in resp.content
