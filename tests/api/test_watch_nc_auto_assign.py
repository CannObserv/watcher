"""Integration tests for auto-assignment of NC templates on watch create."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from ulid import ULID

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef

pytestmark = pytest.mark.integration

VALID_URL = "json://hooks.example.com/notify"


async def test_watch_create_assigns_global_default_template(client: AsyncClient, db_session):
    """A global-default template is auto-assigned to a newly created watch."""
    # Create a global default template
    tpl = NotificationTemplate(
        title="Global Slack",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        is_global_default=True,
        is_active=True,
    )
    db_session.add(tpl)
    await db_session.commit()

    # Create a watch
    resp = await client.post(
        "/api/v1/watches",
        json={
            "name": "Auto-assign Test",
            "url": "https://example.com",
            "content_type": "html",
        },
    )
    assert resp.status_code == 201
    watch_id = resp.json()["id"]

    # Verify the ref was created
    result = await db_session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert len(refs) == 1
    assert str(refs[0].template_id) == str(tpl.id)


async def test_watch_create_inactive_global_default_is_still_assigned(
    client: AsyncClient, db_session
):
    """Inactive global-default templates are still assigned — inactivity is temporary."""
    tpl = NotificationTemplate(
        title="Inactive Template",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        is_global_default=True,
        is_active=False,
    )
    db_session.add(tpl)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/watches",
        json={
            "name": "Inactive-assign Test",
            "url": "https://inactive-test.example.com",
            "content_type": "html",
        },
    )
    assert resp.status_code == 201
    watch_id = resp.json()["id"]

    result = await db_session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert len(refs) == 1
    assert str(refs[0].template_id) == str(tpl.id)


async def test_watch_create_assigns_domain_default_template(client: AsyncClient, db_session):
    """A domain-specific NC template is assigned when the watch resolves to that domain."""
    from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain

    # Ensure domain row exists (mock probe creates effective_domain from hostname)
    domain_name = "domain-test.example.com"
    domain = Domain(
        name=domain_name,
        min_interval=DEFAULT_MIN_INTERVAL,
        max_concurrency=DEFAULT_MAX_CONCURRENCY,
        current_interval=DEFAULT_MIN_INTERVAL,
    )
    db_session.add(domain)

    tpl = NotificationTemplate(
        title="Domain Template",
        apprise_url=encrypt_apprise_url(VALID_URL),
        channel_hint="json",
        events=["change_detected"],
        is_global_default=False,
        is_active=True,
    )
    db_session.add(tpl)
    await db_session.flush()

    ref = DomainNcRef(domain_name=domain_name, template_id=tpl.id)
    db_session.add(ref)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/watches",
        json={
            "name": "Domain Auto-assign Test",
            "url": f"https://{domain_name}/page",
            "content_type": "html",
        },
    )
    assert resp.status_code == 201
    watch_id = resp.json()["id"]

    result = await db_session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert len(refs) == 1
    assert str(refs[0].template_id) == str(tpl.id)
