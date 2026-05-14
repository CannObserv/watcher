"""E2E: scheduled fetch → scratch → POST → cascade → outbox interlock → drain.

Exercises the full Phase 5 chain with in-process mocks:
  1. _run_check_pipeline produces root + fragment SourceRevisions (3 POSTs).
  2. POST failure → outbox row → drain recovers → row deleted.

Archiver SDK is mocked; no live Archiver server required.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.pending_source_revision import PendingSourceRevision
from src.core.sources.resolver import ResolvedFragmentSource, ResolvedRootSource
from src.workers import source_revisions_drain as drain_mod
from src.workers.pipeline import _run_check_pipeline
from src.workers.source_revisions_drain import drain_pending_source_revisions
from tests.conftest import make_info_source, make_watch

pytestmark = pytest.mark.integration


def _async_session_factory_returning(db_session: AsyncSession):
    """Return a fake session-factory that yields the given test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


# ---------------------------------------------------------------------------
# Test 1: Root + 2 fragments → 3 POSTs, 3 scratch files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_fetch_produces_root_plus_fragments(db_session, tmp_path, monkeypatch):
    """One pipeline tick produces 1 root + 2 fragment SourceRevisions (3 total POSTs)."""
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "600")

    # Create real DB rows for info_sources (FK required on Watch).
    root_source = await make_info_source(db_session, url="https://example.com")
    frag1_source = await make_info_source(
        db_session, parent_info_source_id=str(root_source.info_source_id)
    )
    frag2_source = await make_info_source(
        db_session, parent_info_source_id=str(root_source.info_source_id)
    )
    root_id = str(root_source.info_source_id)
    frag1_id = str(frag1_source.info_source_id)
    frag2_id = str(frag2_source.info_source_id)

    watch = await make_watch(db_session, info_source_id=root_source.info_source_id)

    resolved = ResolvedRootSource(
        info_source_id=root_id,
        url="https://example.com",
        source_spec={
            "target": {"url": "https://example.com"},
            "extraction": {"algorithm": "full_page"},
        },
        children=[
            ResolvedFragmentSource(
                info_source_id=frag1_id,
                parent_info_source_id=root_id,
                source_spec={"extraction": {"algorithm": "css", "selector": "#a"}},
            ),
            ResolvedFragmentSource(
                info_source_id=frag2_id,
                parent_info_source_id=root_id,
                source_spec={"extraction": {"algorithm": "css", "selector": "#b"}},
            ),
        ],
    )

    fake_client = MagicMock()
    fake_client.post_source_revision = AsyncMock(
        side_effect=[
            MagicMock(source_revision_id="01JREVREVREVREVREVREVREVRE"),
            MagicMock(source_revision_id="01JFREV1FREV1FREV1FREV1FRE"),
            MagicMock(source_revision_id="01JFREV2FREV2FREV2FREV2FRE"),
        ]
    )

    result = await _run_check_pipeline(
        watch=watch,
        raw_content=b"<html><body><div id='a'>sect-a</div><div id='b'>sect-b</div></body></html>",
        fetcher_used="http",
        fetch_duration_ms=10,
        storage=None,
        session=db_session,
        resolved=resolved,
        info_client=fake_client,
    )

    assert fake_client.post_source_revision.await_count == 3
    assert len(list(tmp_path.glob("*.bin"))) == 3
    assert result["is_changed"] is True
    assert len(result["fragment_revision_ids"]) == 2


# ---------------------------------------------------------------------------
# Test 2: POST failure → outbox row → drain recovers → row deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_drains_after_archiver_recovery(db_session, tmp_path, monkeypatch):
    """Pipeline POST fails → outbox row → drain succeeds → row deleted, dispatch fires."""
    monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "600")

    root_source = await make_info_source(db_session, url="https://example.com")
    root_id = str(root_source.info_source_id)
    watch = await make_watch(db_session, info_source_id=root_source.info_source_id)

    resolved = ResolvedRootSource(
        info_source_id=root_id,
        url="https://example.com",
        source_spec={
            "target": {"url": "https://example.com"},
            "extraction": {"algorithm": "full_page"},
        },
        children=[],
    )

    failing_client = MagicMock()
    failing_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("down"))

    # First pass: POST fails → outbox row.
    result = await _run_check_pipeline(
        watch=watch,
        raw_content=b"<html><body><p>content</p></body></html>",
        fetcher_used="http",
        fetch_duration_ms=10,
        storage=None,
        session=db_session,
        resolved=resolved,
        info_client=failing_client,
    )
    assert result.get("outbox") is True
    await db_session.commit()

    # Verify outbox has the row.
    pending = (await db_session.execute(select(PendingSourceRevision))).scalars().all()
    assert len(pending) == 1
    row = pending[0]

    # Second pass: drain with recovered client.
    recovered_client = MagicMock()
    recovered_client.post_source_revision = AsyncMock(
        return_value=MagicMock(
            source_revision_id=str(row.id),
            content_fingerprint=row.content_fingerprint,
        )
    )
    fake_dispatch = AsyncMock()

    monkeypatch.setattr(
        drain_mod,
        "get_session_factory",
        lambda: _async_session_factory_returning(db_session),
    )
    monkeypatch.setattr(drain_mod, "_get_archiver_client", lambda: recovered_client)
    monkeypatch.setattr(drain_mod, "dispatch_event_notifications", fake_dispatch)

    drain_result = await drain_pending_source_revisions(batch_size=10)
    assert drain_result["drained"] == 1
    assert drain_result["failed"] == 0

    # Outbox empty.
    remaining = (await db_session.execute(select(PendingSourceRevision))).scalars().all()
    assert len(remaining) == 0

    # dispatch was called (watch exists for this info_source_id).
    fake_dispatch.assert_awaited_once()
