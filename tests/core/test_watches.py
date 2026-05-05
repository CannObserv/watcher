"""Integration tests for the create_watch service function.

Phase 2c contract: ``create_watch`` takes ``info_item_id`` (not ``url``) and
calls the SDK to validate the InfoItem + resolve the URL from the primary
InfoSpec. The probe still runs against the resolved URL to populate
``effective_url`` / ``effective_domain``.
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
from tests.conftest import make_info_item, make_info_spec, make_watch

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


def _make_info_client(*, info_item_id: str, url: str = "https://example.com/page"):
    """Build an SDK mock that returns a stable InfoItem + primary spec."""
    fake_item = MagicMock()
    fake_item.info_item_id = info_item_id

    fake_spec = MagicMock()
    fake_spec.info_item_id = info_item_id
    fake_spec.info_spec_id = "01TESTSPEC00000000000000XX"
    fake_spec_doc = MagicMock()
    fake_spec_doc.to_dict = MagicMock(
        return_value={
            "schema_version": 1,
            "target": {"url": url},
            "extraction": {"algorithm": "full_page"},
            "fingerprint": {"algorithm": "simhash"},
        }
    )
    fake_spec.document = fake_spec_doc

    client = MagicMock()
    client.get_info_item = AsyncMock(return_value=fake_item)
    client.get_primary_info_spec = AsyncMock(return_value=fake_spec)
    return client


async def _seed_info(db_session, *, name="Test Item", url="https://example.com/page"):
    """Create an InfoItem + InfoSpec; commit; return id (str)."""
    item = await make_info_item(db_session, name=name)
    await make_info_spec(db_session, item, url=url)
    await db_session.commit()
    return str(item.info_item_id)


class TestCreateWatch:
    async def test_returns_committed_watch(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://example.com/page"
            ),
            name="Test Watch",
            info_item_id=info_item_id,
            content_type="html",
        )
        assert isinstance(watch, Watch)
        assert watch.id is not None
        assert watch.name == "Test Watch"
        assert str(watch.info_item_id) == info_item_id

    async def test_sets_effective_url_and_domain(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://example.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(
                url="https://example.com/canonical",
                domain="example.com",
            ),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://example.com/page"
            ),
            name="Test Watch",
            info_item_id=info_item_id,
            content_type="html",
        )
        assert watch.effective_url == "https://example.com/canonical"
        assert watch.effective_domain == "example.com"

    async def test_creates_domain_for_new_domain(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://newsite.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="newsite.gov"),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://newsite.gov/page"
            ),
            name="Gov Watch",
            info_item_id=info_item_id,
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

        info_item_id = await _seed_info(db_session, url="https://existing.gov/page")
        await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="existing.gov"),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://existing.gov/page"
            ),
            name="Existing Domain Watch",
            info_item_id=info_item_id,
            content_type="html",
        )
        result = await db_session.execute(select(Domain).where(Domain.name == "existing.gov"))
        assert len(result.scalars().all()) == 1

    async def test_creates_audit_log_with_watch_id(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://audit-test.com/page")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(domain="audit-test.com"),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://audit-test.com/page"
            ),
            name="Audit Watch",
            info_item_id=info_item_id,
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
        assert entry.payload["info_item_id"] == info_item_id

    async def test_raises_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        info_item_id = await _seed_info(db_session, url="https://broken.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=_make_info_client(
                    info_item_id=info_item_id, url="https://broken.example"
                ),
                name="Bad URL Watch",
                info_item_id=info_item_id,
                content_type="html",
            )

    async def test_no_watch_created_on_probe_failure(self, db_session):
        async def failing_probe(url: str) -> ProbeResult:
            raise httpx.ConnectError("unreachable")

        info_item_id = await _seed_info(db_session, url="https://broken2.example")
        with pytest.raises(httpx.HTTPError):
            await create_watch(
                session=db_session,
                probe_fn=failing_probe,
                info_client=_make_info_client(
                    info_item_id=info_item_id, url="https://broken2.example"
                ),
                name="Bad URL Watch",
                info_item_id=info_item_id,
                content_type="html",
            )

        result = await db_session.execute(select(Watch).where(Watch.name == "Bad URL Watch"))
        assert result.scalar_one_or_none() is None

    async def test_dispatches_watch_created_notification(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://notify-test.com/page")
        with patch(
            "src.core.watches.dispatch_event_notifications",
            new_callable=AsyncMock,
        ) as mock_dispatch:
            watch = await create_watch(
                session=db_session,
                probe_fn=_make_probe(domain="notify-test.com"),
                info_client=_make_info_client(
                    info_item_id=info_item_id, url="https://notify-test.com/page"
                ),
                name="Notify Watch",
                info_item_id=info_item_id,
                content_type="html",
            )
            mock_dispatch.assert_awaited_once()
            call_kwargs = mock_dispatch.call_args.kwargs
            assert call_kwargs["session"] is db_session
            assert call_kwargs["event"].watch_id == str(watch.id)
            # watch_url comes from the resolved InfoSpec
            assert call_kwargs["event"].watch_url == "https://notify-test.com/page"

    async def test_passes_schedule_config_through(self, db_session):
        info_item_id = await _seed_info(db_session, url="https://example.com/configured")
        watch = await create_watch(
            session=db_session,
            probe_fn=_make_probe(),
            info_client=_make_info_client(
                info_item_id=info_item_id, url="https://example.com/configured"
            ),
            name="Configured Watch",
            info_item_id=info_item_id,
            content_type="pdf",
            schedule_config={"interval": "6h"},
        )
        assert watch.content_type == "pdf"
        assert watch.schedule_config == {"interval": "6h"}


class TestResolveWatchUrl:
    """resolve_watch_url returns the URL from the watch's primary InfoSpec."""

    async def test_returns_target_url_from_primary_info_spec(self, db_session):
        """The resolved URL comes from the spec's `target.url`, not any Watch column."""
        watch = await make_watch(db_session, url="https://stale-or-missing.example.com")

        fake_client = MagicMock()
        fake_spec = MagicMock()
        fake_spec.info_item_id = str(watch.info_item_id)
        fake_spec.info_spec_id = "01XYZ"
        fake_spec.document = {"target": {"url": "https://from-spec.example.com"}}
        fake_client.get_primary_info_spec = AsyncMock(return_value=fake_spec)

        resolved_url = await resolve_watch_url(watch, fake_client)

        assert resolved_url == "https://from-spec.example.com"
        fake_client.get_primary_info_spec.assert_awaited_once_with(
            str(watch.info_item_id), force_refresh=False
        )
