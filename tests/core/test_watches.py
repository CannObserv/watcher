"""Integration tests for the create_watch service function.

Phase 5 contract: ``create_watch`` takes ``info_source_id`` (required).
URL resolution uses ``get_info_source`` which returns ``source_spec.additional_properties``.
The probe runs against the resolved URL to populate ``effective_url`` / ``effective_domain``.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.probe import ProbeResult
from src.core.watches import create_watch, resolve_watch_url
from tests.conftest import make_info_item, make_info_source, make_watch

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


def _make_info_client(*, url: str = "https://example.com/page"):
    """Build an SDK mock that returns a stable InfoSource for URL resolution."""
    fake_source_spec = MagicMock()
    fake_source_spec.additional_properties = {"target": {"url": url}}
    fake_source = MagicMock()
    fake_source.info_source_id = "01TESTSOURCE0000000000000X"
    fake_source.parent_info_source_id = None
    fake_source.source_spec = fake_source_spec

    client = MagicMock()
    client.get_info_source = AsyncMock(return_value=fake_source)
    return client


async def _seed_info(db_session, *, name="Test Item", url="https://example.com/page"):
    """Create an InfoItem + InfoSource; commit; return (info_item_id, info_source_id) as str.

    Phase 5: create_watch resolves URL via get_info_source. The _make_info_client
    mock stubs this — url param here controls what the mock returns.
    """
    item = await make_info_item(db_session, name=name)
    source = await make_info_source(db_session, url=url)
    await db_session.commit()
    return str(item.info_item_id), str(source.info_source_id)


class TestCreateWatch:
    async def test_returns_committed_watch(self, db_session):
        info_item_id, info_source_id = await _seed_info(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            info_client=_make_info_client(url="https://example.com/page"),
            name="Test Watch",
            info_source_id=info_source_id,
            content_type="html",
        )
        assert isinstance(watch, Watch)
        assert watch.id is not None
        assert watch.name == "Test Watch"
        assert str(watch.info_source_id) == info_source_id

    async def test_sets_effective_url_and_domain(self, db_session):
        info_item_id, info_source_id = await _seed_info(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(
                url="https://example.com/canonical",
                domain="example.com",
            ),
            info_client=_make_info_client(url="https://example.com/page"),
            name="Test Watch",
            info_source_id=info_source_id,
            content_type="html",
        )
        assert watch.effective_url == "https://example.com/canonical"
        assert watch.effective_domain == "example.com"

    async def test_creates_domain_for_new_domain(self, db_session):
        info_item_id, info_source_id = await _seed_info(db_session, url="https://newsite.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="newsite.gov"),
            info_client=_make_info_client(url="https://newsite.gov/page"),
            name="Gov Watch",
            info_source_id=info_source_id,
            content_type="html",
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "newsite.gov"))
        domain = result.scalar_one_or_none()
        assert domain is not None
        assert domain.is_active is True

    async def test_skips_domain_creation_if_exists(self, db_session):
        existing = Domain(name="existing.gov")
        db_session.add(existing)
        await db_session.flush()

        info_item_id, info_source_id = await _seed_info(db_session, url="https://existing.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="existing.gov"),
            info_client=_make_info_client(url="https://existing.gov/page"),
            name="Existing Domain Watch",
            info_source_id=info_source_id,
            content_type="html",
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "existing.gov"))
        assert len(result.scalars().all()) == 1

    async def test_creates_audit_log_with_watch_id(self, db_session):
        info_item_id, info_source_id = await _seed_info(
            db_session, url="https://audit-test.com/page"
        )
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="audit-test.com"),
            info_client=_make_info_client(url="https://audit-test.com/page"),
            name="Audit Watch",
            info_source_id=info_source_id,
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
        assert entry.payload["effective_domain"] == "audit-test.com"
        assert entry.payload["info_source_id"] == info_source_id

    async def test_raises_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        info_item_id, info_source_id = await _seed_info(db_session, url="https://broken.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=_make_info_client(url="https://broken.example"),
                name="Bad URL Watch",
                info_source_id=info_source_id,
                content_type="html",
            )

    async def test_no_watch_created_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        info_item_id, info_source_id = await _seed_info(db_session, url="https://broken2.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=_make_info_client(url="https://broken2.example"),
                name="Bad URL Watch",
                info_source_id=info_source_id,
                content_type="html",
            )

        result = await db_session.execute(select(Watch).where(Watch.name == "Bad URL Watch"))
        assert result.scalar_one_or_none() is None

    async def test_dispatches_watch_created_notification(self, db_session):
        info_item_id, info_source_id = await _seed_info(
            db_session, url="https://notify-test.com/page"
        )
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                probe_fn=_make_probe(domain="notify-test.com"),
                info_client=_make_info_client(url="https://notify-test.com/page"),
                name="Notify Watch",
                info_source_id=info_source_id,
                content_type="html",
            )
            mock_dispatch.assert_awaited_once()
            call_kwargs = mock_dispatch.call_args.kwargs
            assert call_kwargs["session"] is db_session
            assert call_kwargs["event"].watch_id == str(watch.id)
            # watch_url comes from the resolved InfoSpec
            assert call_kwargs["event"].watch_url == "https://notify-test.com/page"

    async def test_passes_schedule_config_through(self, db_session):
        info_item_id, info_source_id = await _seed_info(
            db_session, url="https://example.com/configured"
        )
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            info_client=_make_info_client(url="https://example.com/configured"),
            name="Configured Watch",
            info_source_id=info_source_id,
            content_type="pdf",
            schedule_config={"interval": "6h"},
        )
        assert watch.content_type == "pdf"
        assert watch.schedule_config == {"interval": "6h"}


class TestCreateWatchFragmentUrl:
    async def test_create_watch_resolves_fragment_to_root_url(self, db_session):
        """A fragment Watch's effective_url is the chain's root URL."""
        from unittest.mock import AsyncMock, MagicMock, patch

        # Seed info rows so conftest/DB setup passes
        info_item_id, info_source_id = await _seed_info(
            db_session, url="https://root.example.com/page"
        )

        # Build a client that returns the fragment first, then the root
        frag_spec = MagicMock()
        frag_spec.additional_properties = {"extraction": {"algorithm": "css", "selector": "#x"}}
        frag_source = MagicMock()
        frag_source.info_source_id = "01HZZ00000000000000FRAGMENT"
        frag_source.parent_info_source_id = "01HZZ00000000000000000ROOT"
        frag_source.source_spec = frag_spec

        root_spec = MagicMock()
        root_spec.additional_properties = {"target": {"url": "https://root.example.com/page"}}
        root_source = MagicMock()
        root_source.info_source_id = "01HZZ00000000000000000ROOT"
        root_source.parent_info_source_id = None
        root_source.source_spec = root_spec

        client = MagicMock()
        client.get_info_source = AsyncMock(side_effect=[frag_source, root_source])

        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ):
            watch = await create_watch(
                session=db_session,
                probe_fn=_make_probe(url="https://root.example.com/page"),
                info_client=client,
                name="Fragment Watch",
                info_source_id=info_source_id,
                content_type="html",
            )

        assert watch.effective_url == "https://root.example.com/page"
        assert watch.effective_domain == "root.example.com"
        # Two get_info_source calls: fragment + root walk
        assert client.get_info_source.call_count == 2


class TestResolveWatchUrl:
    """resolve_watch_url returns the URL from the watch's primary InfoSource spec."""

    async def test_returns_target_url_from_info_source(self, db_session):
        """The resolved URL comes from the InfoSource's source_spec target.url."""
        watch = await make_watch(db_session, url="https://stale-or-missing.example.com")

        fake_source_spec = MagicMock()
        fake_source_spec.additional_properties = {
            "target": {"url": "https://from-source.example.com"}
        }
        fake_source = MagicMock()
        fake_source.source_spec = fake_source_spec

        fake_client = MagicMock()
        fake_client.get_info_source = AsyncMock(return_value=fake_source)

        resolved_url = await resolve_watch_url(watch, fake_client)

        assert resolved_url == "https://from-source.example.com"
        fake_client.get_info_source.assert_awaited_once_with(str(watch.info_source_id))
