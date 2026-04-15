"""Integration tests for the create_watch service function."""

from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.watch import Watch
from src.core.probe import ProbeResult
from src.core.watches import create_watch

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


class TestCreateWatch:
    async def test_returns_committed_watch(self, db_session):
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            name="Test Watch",
            url="https://example.com/page",
            content_type="html",
            schedule_config={},
            fetch_config={},
        )
        assert isinstance(watch, Watch)
        assert watch.id is not None
        assert watch.name == "Test Watch"
        assert watch.url == "https://example.com/page"

    async def test_sets_effective_url_and_domain(self, db_session):
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(
                url="https://example.com/canonical",
                domain="example.com",
            ),
            name="Test Watch",
            url="https://example.com/page",
            content_type="html",
            schedule_config={},
            fetch_config={},
        )
        assert watch.effective_url == "https://example.com/canonical"
        assert watch.effective_domain == "example.com"

    async def test_creates_domain_for_new_domain(self, db_session):
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="newsite.gov"),
            name="Gov Watch",
            url="https://newsite.gov/page",
            content_type="html",
            schedule_config={},
            fetch_config={},
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "newsite.gov"))
        domain = result.scalar_one_or_none()
        assert domain is not None
        assert domain.is_active is True

    async def test_skips_domain_creation_if_exists(self, db_session):
        existing = Domain(name="existing.gov")
        db_session.add(existing)
        await db_session.flush()

        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="existing.gov"),
            name="Existing Domain Watch",
            url="https://existing.gov/page",
            content_type="html",
            schedule_config={},
            fetch_config={},
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "existing.gov"))
        assert len(result.scalars().all()) == 1

    async def test_creates_audit_log_with_watch_id(self, db_session):
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="audit-test.com"),
            name="Audit Watch",
            url="https://audit-test.com/page",
            content_type="html",
            schedule_config={},
            fetch_config={},
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

    async def test_raises_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                name="Bad URL Watch",
                url="https://broken.example",
                content_type="html",
                schedule_config={},
                fetch_config={},
            )

    async def test_no_watch_created_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                name="Bad URL Watch",
                url="https://broken2.example",
                content_type="html",
                schedule_config={},
                fetch_config={},
            )

        result = await db_session.execute(
            select(Watch).where(Watch.url == "https://broken2.example")
        )
        assert result.scalar_one_or_none() is None

    async def test_dispatches_watch_created_notification(self, db_session):
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                probe_fn=_make_probe(domain="notify-test.com"),
                name="Notify Watch",
                url="https://notify-test.com/page",
                content_type="html",
                schedule_config={},
                fetch_config={},
            )
            mock_dispatch.assert_awaited_once()
            call_kwargs = mock_dispatch.call_args.kwargs
            assert call_kwargs["session"] is db_session
            assert call_kwargs["event"].watch_id == str(watch.id)

    async def test_passes_schedule_and_fetch_config(self, db_session):
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            name="Configured Watch",
            url="https://example.com/configured",
            content_type="pdf",
            schedule_config={"interval": "6h"},
            fetch_config={"timeout": 30},
        )
        assert watch.content_type == "pdf"
        assert watch.schedule_config == {"interval": "6h"}
        assert watch.fetch_config == {"timeout": 30}
