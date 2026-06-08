"""Integration tests for the create_watch service function (InfoItem-first, #160).

#185 Phase A step 6: Watch no longer stores info_item_id, effective_url, or
target_info_source_id. These live on WatchedItem. create_watch probes the URL
and sets watched_item.effective_url (first Watch wins).
"""

from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.probe import ProbeResult
from src.core.watches import create_watch, resolve_watch_url
from tests.conftest import (
    bind_primary_source,
    make_info_item,
    make_info_source,
    make_watch,
)

pytestmark = pytest.mark.integration


def _make_probe(url: str | None = None, domain: str | None = None):
    """Return a mock probe_fn that resolves without real HTTP calls."""

    async def probe_fn(target_url: str) -> ProbeResult:
        resolved = url or target_url
        resolved_domain = domain or urlparse(resolved).hostname or ""
        return ProbeResult(
            effective_url=resolved,
            effective_domain=resolved_domain,
            redirect_chain=[target_url],
            status_code=200,
            content_type="text/html",
        )

    return probe_fn


async def _seed_item_with_primary(db_session, *, name="Test Item", url="https://example.com/page"):
    """Create an InfoItem + primary InfoSource binding; return (item, source)."""
    item = await make_info_item(db_session, name=name)
    source = await make_info_source(db_session, url=url)
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=source.info_source_id,
    )
    await db_session.commit()
    return item, source


class TestCreateWatch:
    async def test_returns_committed_watch(self, db_session, info_client):
        item, _ = await _seed_item_with_primary(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            info_client=info_client,
            name="Test Watch",
            info_item_id=str(item.info_item_id),
            content_type="html",
        )
        assert isinstance(watch, Watch)
        assert watch.id is not None
        assert watch.name == "Test Watch"
        assert watch.watched_item.info_item_id == item.info_item_id

    async def test_sets_effective_url_on_watched_item(self, db_session, info_client):
        """create_watch populates watched_item.effective_url from probe result."""
        item, _ = await _seed_item_with_primary(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(
                url="https://example.com/canonical",
                domain="example.com",
            ),
            info_client=info_client,
            name="Test Watch",
            info_item_id=str(item.info_item_id),
            content_type="html",
        )
        assert watch.watched_item.effective_url == "https://example.com/canonical"
        assert watch.watched_item.domain_name == "example.com"

    async def test_creates_domain_for_new_domain(self, db_session, info_client):
        item, _ = await _seed_item_with_primary(db_session, url="https://newsite.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="newsite.gov"),
            info_client=info_client,
            name="Gov Watch",
            info_item_id=str(item.info_item_id),
            content_type="html",
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "newsite.gov"))
        domain = result.scalar_one_or_none()
        assert domain is not None
        assert domain.is_active is True

    async def test_skips_domain_creation_if_exists(self, db_session, info_client):
        existing = Domain(name="existing.gov")
        db_session.add(existing)
        await db_session.flush()

        item, _ = await _seed_item_with_primary(db_session, url="https://existing.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="existing.gov"),
            info_client=info_client,
            name="Existing Domain Watch",
            info_item_id=str(item.info_item_id),
            content_type="html",
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "existing.gov"))
        assert len(result.scalars().all()) == 1

    async def test_creates_audit_log_with_watch_id(self, db_session, info_client):
        item, _ = await _seed_item_with_primary(db_session, url="https://audit-test.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="audit-test.com"),
            info_client=info_client,
            name="Audit Watch",
            info_item_id=str(item.info_item_id),
            content_type="html",
        )
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == EventType.WATCH_CREATED,
                AuditLog.watch_id == str(watch.id),
            )
        )
        entry = result.scalar_one()
        assert entry.watch_id == str(watch.id)
        assert entry.payload["name"] == "Audit Watch"
        assert entry.payload["domain_name"] == "audit-test.com"
        assert entry.payload["info_item_id"] == str(item.info_item_id)

    async def test_raises_on_probe_failure(self, db_session, info_client):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        item, _ = await _seed_item_with_primary(db_session, url="https://broken.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=info_client,
                name="Bad URL Watch",
                info_item_id=str(item.info_item_id),
                content_type="html",
            )

    async def test_no_watch_created_on_probe_failure(self, db_session, info_client):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        item, _ = await _seed_item_with_primary(db_session, url="https://broken2.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=info_client,
                name="Bad URL Watch",
                info_item_id=str(item.info_item_id),
                content_type="html",
            )

        result = await db_session.execute(select(Watch).where(Watch.name == "Bad URL Watch"))
        assert result.scalar_one_or_none() is None

    async def test_dispatches_watch_created_notification(self, db_session, info_client):
        item, _ = await _seed_item_with_primary(db_session, url="https://notify-test.com/page")
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                probe_fn=_make_probe(domain="notify-test.com"),
                info_client=info_client,
                name="Notify Watch",
                info_item_id=str(item.info_item_id),
                content_type="html",
            )
            mock_dispatch.assert_awaited_once()
            call_kwargs = mock_dispatch.call_args.kwargs
            assert call_kwargs["session"] is db_session
            assert call_kwargs["event"].watch_id == str(watch.id)
            # watch_url comes from the InfoItem's primary URL.
            assert call_kwargs["event"].watch_url == "https://notify-test.com/page"


class TestResolveWatchUrl:
    """resolve_watch_url resolves via watched_item.info_item_id (deprecated helper)."""

    async def test_returns_primary_url_via_watched_item(self, db_session, info_client):
        item, _ = await _seed_item_with_primary(
            db_session, url="https://from-info-item.example.com"
        )
        watch = await make_watch(db_session, info_item_id=item.info_item_id)
        await db_session.refresh(watch, ["watched_item"])

        resolved_url = await resolve_watch_url(watch, info_client)

        assert resolved_url == "https://from-info-item.example.com"


# ---------------------------------------------------------------------------
# Phase 6 / #160: InfoItem-first create_watch tests.
#
# These exercise the new contract — `info_item_id` (required) + optional
# `target_info_source_id` (must be a sub_aspect binding). They use the
# conftest `info_client` fixture (DB-backed mock) and a local probe_fn.
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_fn():
    """Probe stub that mirrors target URL back as effective_url."""

    async def _probe(url: str) -> ProbeResult:
        host = urlparse(url).hostname or ""
        return ProbeResult(
            effective_url=url,
            effective_domain=host,
            redirect_chain=[url],
            status_code=200,
            content_type="text/html",
        )

    return _probe


async def test_create_watch_info_item_first_auto_creates_watched_item(
    db_session, info_client, probe_fn
):
    """Happy path: create_watch with info_item_id only; URL resolves; WatchedItem auto-created."""
    item = await make_info_item(db_session)
    primary = await make_info_source(db_session, url="https://example.com/registry")
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=primary.info_source_id,
    )

    watch = await create_watch(
        session=db_session,
        probe_fn=probe_fn,
        info_client=info_client,
        name="OR registry",
        info_item_id=str(item.info_item_id),
    )

    # Phase A step 6: info_item_id + effective_url live on WatchedItem, not Watch.
    assert watch.watched_item_id is not None
    assert watch.watched_item.info_item_id == item.info_item_id
    assert watch.watched_item.effective_url is not None
    assert watch.watched_item.domain_name is not None
    # WatchedItem.name falls back to the Watch's name when auto-created.
    assert watch.watched_item.name == "OR registry"


async def test_create_watch_attaches_to_existing_watched_item(db_session, info_client, probe_fn):
    """Two Watches on the same InfoItem share a WatchedItem (auto-attach)."""
    item = await make_info_item(db_session)
    primary = await make_info_source(db_session, url="https://example.com/x")
    await bind_primary_source(
        db_session,
        info_item_id=item.info_item_id,
        info_source_id=primary.info_source_id,
    )

    w1 = await create_watch(
        session=db_session,
        probe_fn=probe_fn,
        info_client=info_client,
        name="First",
        info_item_id=str(item.info_item_id),
    )
    w2 = await create_watch(
        session=db_session,
        probe_fn=probe_fn,
        info_client=info_client,
        name="Second",
        info_item_id=str(item.info_item_id),
    )
    assert w1.watched_item_id == w2.watched_item_id
    # First watch's name wins for the WatchedItem fallback name.
    assert w1.watched_item.name == "First"
