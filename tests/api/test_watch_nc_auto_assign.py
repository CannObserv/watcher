"""Integration tests verifying that watch creation no longer seeds WatchNcRef.

Global and domain templates are now dispatched via live lookup in notify.py,
so no WatchNcRef rows should be created at watch creation time.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from ulid import ULID

from src.core.crypto import encrypt_apprise_url
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from tests.conftest import make_watch

pytestmark = pytest.mark.integration

VALID_URL = "json://hooks.example.com/notify"


async def test_watch_create_does_not_seed_global_default_template(client: AsyncClient, db_session):
    """Creating a watch does NOT auto-assign global-default templates via WatchNcRef.

    Global templates now dispatch via live lookup in notify.py.
    """
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

    watch = await make_watch(
        db_session, name="No-Seed Test", url="https://example.com", content_type="html"
    )
    await db_session.commit()
    watch_id = str(watch.id)

    result = await db_session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert refs == [], "WatchNcRef should not be seeded at watch creation"


async def test_watch_create_does_not_seed_domain_default_template(client: AsyncClient, db_session):
    """Creating a watch under a domain does NOT auto-assign DomainNcRef templates.

    Domain templates now dispatch via live lookup in notify.py.
    """
    from src.core.models.domain import DEFAULT_MAX_CONCURRENCY, DEFAULT_MIN_INTERVAL, Domain

    domain_name = "no-seed-domain.example.com"
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

    db_session.add(DomainNcRef(domain_name=domain_name, template_id=tpl.id))
    await db_session.commit()

    watch = await make_watch(
        db_session,
        name="Domain No-Seed Test",
        url=f"https://{domain_name}/page",
        content_type="html",
        effective_domain=domain_name,
    )
    await db_session.commit()
    watch_id = str(watch.id)

    result = await db_session.execute(
        select(WatchNcRef).where(WatchNcRef.watch_id == ULID.from_str(watch_id))
    )
    refs = result.scalars().all()
    assert refs == [], "WatchNcRef should not be seeded from domain defaults at watch creation"
