"""Tests for _run_check_pipeline and helpers in workers.pipeline."""

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from src.core.models.pending_source_revision import PendingSourceRevision
from src.core.sources.revision_cache import upsert_last_known
from src.workers.pipeline import (
    _extract_with_spec,
    _extraction_config_from_spec,
    _run_check_pipeline,
)
from tests.conftest import make_info_source, make_watch
from tests.workers.conftest import make_resolved


class TestExtractionConfigFromSpec:
    def test_full_page_yields_empty_selectors(self):
        config = _extraction_config_from_spec({"extraction": {"algorithm": "full_page"}})
        assert config == {"selectors": []}

    def test_css_selector_yields_single_selector_list(self):
        config = _extraction_config_from_spec(
            {"extraction": {"algorithm": "css", "selector": ".target"}}
        )
        assert config == {"selectors": [".target"]}

    def test_missing_extraction_block_defaults_to_full_page(self):
        config = _extraction_config_from_spec({})
        assert config == {"selectors": []}


class TestExtractWithSpec:
    async def test_extracts_html_with_full_page_algorithm(self):
        document = {"extraction": {"algorithm": "full_page"}}
        result = await _extract_with_spec(b"<html><body><p>Hello</p></body></html>", document)
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    async def test_css_selector_filters_to_matching_section(self):
        document = {"extraction": {"algorithm": "css", "selector": ".target"}}
        result = await _extract_with_spec(
            b"<html><body><div class='target'>kept</div><div>dropped</div></body></html>",
            document,
        )
        joined = " ".join(c.text for c in result.chunks)
        assert "kept" in joined
        assert "dropped" not in joined


# ---------------------------------------------------------------------------
# New Phase 5 pipeline tests (Task 7.2)
# ---------------------------------------------------------------------------


def _make_info_client(source_revision_id: str = "01HZZ000000000000000000REV") -> MagicMock:
    """Build a mock ArchiverClient with post_source_revision pre-wired."""
    client = MagicMock()
    client.post_source_revision = AsyncMock(
        return_value=MagicMock(source_revision_id=source_revision_id)
    )
    return client


@pytest.mark.integration
class TestRunCheckPipeline:
    """New POST-driven pipeline (Phase 5, Task 7.2)."""

    async def test_happy_path_writes_scratch_and_posts(self, db_session, tmp_path, monkeypatch):
        """Writes scratch file under WATCHER_CACHE_DIR, POSTs root revision, returns changed."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="Happy", url="https://example.com")
        resolved = make_resolved(info_source_id="01HZZ000000000000000000SRC")
        info_client = _make_info_client("01HZZ000000000000000000REV")

        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hello world</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )

        assert result["is_changed"] is True
        assert result["source_revision_id"] is not None

        # Scratch file written
        bin_files = list(tmp_path.glob("*.bin"))
        assert len(bin_files) == 1

        # POST called once
        info_client.post_source_revision.assert_awaited_once()

    async def test_fast_path_skips_when_fingerprint_matches(
        self, db_session, tmp_path, monkeypatch
    ):
        """Seeded fingerprint matching extraction → skipped, no POST."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        raw_content = b"<html><body><p>Same content</p></body></html>"
        resolved = make_resolved(info_source_id="01HZZ000000000000000000SRC")
        watch = await make_watch(db_session, name="Fast", url="https://example.com")

        # Compute the fingerprint the pipeline will produce
        extraction = await _extract_with_spec(raw_content, resolved.source_spec)
        root_bytes = "\n".join(c.text for c in extraction.chunks).encode()
        expected_fp = "sha256:" + hashlib.sha256(root_bytes).hexdigest()

        # Seed the cache
        now = datetime.now(UTC)
        await upsert_last_known(
            db_session,
            info_source_id=resolved.info_source_id,
            content_fingerprint=expected_fp,
            source_revision_id="01HZZ000000000000000000OLD",
            captured_at=now,
        )

        info_client = _make_info_client()

        result = await _run_check_pipeline(
            watch=watch,
            raw_content=raw_content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=None,
            session=db_session,
            resolved=resolved,
            info_client=info_client,
        )

        assert result["is_changed"] is False
        assert result.get("skipped_reason") == "fast_path"
        info_client.post_source_revision.assert_not_awaited()

    async def test_outbox_on_post_failure(self, db_session, tmp_path, monkeypatch):
        """ConnectError → row in pending_source_revisions, result.outbox=True."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="Outbox", url="https://example.com")
        resolved = make_resolved(info_source_id="01HZZ000000000000000000SRC")

        info_client = MagicMock()
        info_client.post_source_revision = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>Content</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=None,
            session=db_session,
            resolved=resolved,
            info_client=info_client,
        )

        assert result.get("outbox") is True

        rows = (await db_session.execute(select(PendingSourceRevision))).scalars().all()
        assert len(rows) == 1

    async def test_idempotency_reconcile_renames_scratch(self, db_session, tmp_path, monkeypatch):
        """When server returns a different source_revision_id, scratch is renamed to canonical."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="Rename", url="https://example.com")
        resolved = make_resolved(info_source_id="01HZZ000000000000000000SRC")

        canonical_id = "01HZZ000CANONICAL000000CAN"  # 26-char ULID-length
        info_client = _make_info_client(canonical_id)

        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Rename test</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )

        assert result["source_revision_id"] == canonical_id

        # Canonical file exists
        canonical_file = tmp_path / f"{canonical_id}.bin"
        assert canonical_file.exists()

        # Only canonical file, no allocated-id file with a different name
        bin_files = list(tmp_path.glob("*.bin"))
        assert all(f.stem == canonical_id for f in bin_files), (
            f"Expected only canonical file; found: {[f.name for f in bin_files]}"
        )

    async def test_pipeline_updates_last_changed_at_on_change(
        self, db_session, tmp_path, monkeypatch
    ):
        """A detected change updates watch.last_changed_at; fast-path does not."""
        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))

        watch = await make_watch(db_session, name="LCA", url="https://example.com")
        resolved = make_resolved(info_source_id=str(watch.info_source_id))
        assert watch.last_changed_at is None

        info_client = _make_info_client("01HZZ000000000000000000LCA")
        raw_content = b"<html><body><p>Changed</p></body></html>"

        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=raw_content,
                fetcher_used="http",
                fetch_duration_ms=10,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )

        assert result["is_changed"] is True
        assert watch.last_changed_at is not None

        # Fast-path (same content again) must NOT update last_changed_at.
        before = watch.last_changed_at
        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result2 = await _run_check_pipeline(
                watch=watch,
                raw_content=raw_content,
                fetcher_used="http",
                fetch_duration_ms=10,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=info_client,
            )
        assert result2["is_changed"] is False
        assert watch.last_changed_at == before


# ---------------------------------------------------------------------------
# Phase 5 Task 7.3 — fragment cascade tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRunCheckPipelineCascade:
    """Fragment cascade: extract each child from root bytes, POST, reconcile, dispatch."""

    async def test_pipeline_cascades_fragments_from_cached_bytes(
        self, db_session, tmp_path, monkeypatch
    ):
        """Root + 2 fragments → 3 POSTs, 3 scratch files, 2 fragment_revision_ids."""
        from src.core.sources.resolver import ResolvedFragmentSource, ResolvedRootSource

        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "600")

        # Create the root watch (auto-creates a real info_source row).
        watch = await make_watch(db_session, name="Root", url="https://example.com")
        root_source_id = str(watch.info_source_id)

        # Create two real fragment info_source rows (FK-valid).
        frag1_source = await make_info_source(db_session, parent_info_source_id=root_source_id)
        frag2_source = await make_info_source(db_session, parent_info_source_id=root_source_id)
        frag1_id = str(frag1_source.info_source_id)
        frag2_id = str(frag2_source.info_source_id)

        resolved = ResolvedRootSource(
            info_source_id=root_source_id,
            url="https://example.com",
            source_spec={
                "target": {"url": "https://example.com"},
                "extraction": {"algorithm": "full_page"},
            },
            children=[
                ResolvedFragmentSource(
                    info_source_id=frag1_id,
                    parent_info_source_id=root_source_id,
                    source_spec={"extraction": {"algorithm": "css", "selector": "#x"}},
                ),
                ResolvedFragmentSource(
                    info_source_id=frag2_id,
                    parent_info_source_id=root_source_id,
                    source_spec={"extraction": {"algorithm": "css", "selector": "#y"}},
                ),
            ],
        )

        fake_client = MagicMock()
        fake_client.post_source_revision = AsyncMock(
            side_effect=[
                MagicMock(source_revision_id="01HZZ00000000000000000REV"),
                MagicMock(source_revision_id="01HZZ00000000000000FREV1"),
                MagicMock(source_revision_id="01HZZ00000000000000FREV2"),
            ]
        )

        raw = b"<html><body><div id='x'>sect-x</div><div id='y'>sect-y</div></body></html>"
        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=raw,
                fetcher_used="http",
                fetch_duration_ms=10,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=fake_client,
            )

        # 3 POSTs: root + 2 fragments
        assert fake_client.post_source_revision.await_count == 3
        # 3 scratch files (root + 2 fragments)
        bin_files = list(tmp_path.glob("*.bin"))
        assert len(bin_files) == 3
        assert len(result["fragment_revision_ids"]) == 2

    async def test_pipeline_fragment_fast_path_skips_unchanged(
        self, db_session, tmp_path, monkeypatch
    ):
        """Fragment whose fingerprint already matches → no POST for that fragment."""
        from src.core.sources.resolver import ResolvedFragmentSource, ResolvedRootSource

        monkeypatch.setenv("WATCHER_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("WATCHER_CACHE_TTL_SECONDS", "600")

        raw_content = b"<html><body><div id='x'>sect-x</div><div id='y'>sect-y</div></body></html>"
        frag1_spec = {"extraction": {"algorithm": "css", "selector": "#x"}}

        # Pre-compute fragment 1's fingerprint so we can seed the cache.
        frag1_extracted = await _extract_with_spec(raw_content, frag1_spec)
        frag1_bytes = "\n".join(c.text for c in frag1_extracted.chunks).encode()
        frag1_fp = "sha256:" + hashlib.sha256(frag1_bytes).hexdigest()

        watch = await make_watch(db_session, name="Root", url="https://example.com")
        root_source_id = str(watch.info_source_id)

        frag1_source = await make_info_source(db_session, parent_info_source_id=root_source_id)
        frag2_source = await make_info_source(db_session, parent_info_source_id=root_source_id)
        frag1_id = str(frag1_source.info_source_id)
        frag2_id = str(frag2_source.info_source_id)

        # Seed fragment 1's fingerprint → fast-path will skip it.
        await upsert_last_known(
            db_session,
            info_source_id=frag1_id,
            content_fingerprint=frag1_fp,
            source_revision_id="01HZZ00000000000000FOLD1",
            captured_at=datetime.now(UTC),
        )

        resolved = ResolvedRootSource(
            info_source_id=root_source_id,
            url="https://example.com",
            source_spec={
                "target": {"url": "https://example.com"},
                "extraction": {"algorithm": "full_page"},
            },
            children=[
                ResolvedFragmentSource(
                    info_source_id=frag1_id,
                    parent_info_source_id=root_source_id,
                    source_spec=frag1_spec,
                ),
                ResolvedFragmentSource(
                    info_source_id=frag2_id,
                    parent_info_source_id=root_source_id,
                    source_spec={"extraction": {"algorithm": "css", "selector": "#y"}},
                ),
            ],
        )

        fake_client = MagicMock()
        fake_client.post_source_revision = AsyncMock(
            side_effect=[
                MagicMock(source_revision_id="01HZZ00000000000000000REV"),
                MagicMock(source_revision_id="01HZZ00000000000000FREV2"),
            ]
        )

        with patch("src.core.notifications.notify.dispatch_event_notifications", new=AsyncMock()):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=raw_content,
                fetcher_used="http",
                fetch_duration_ms=10,
                storage=None,
                session=db_session,
                resolved=resolved,
                info_client=fake_client,
            )

        # Only 2 POSTs: root + frag2 (frag1 skipped via fast-path).
        assert fake_client.post_source_revision.await_count == 2
        # Only 1 fragment revision committed (frag2).
        assert len(result["fragment_revision_ids"]) == 1


# Phase 5 (#156): TestModifiedMetadataInvariant and TestComputeSignificance removed —
# differ.py and _compute_significance deleted in Phase 5 cutover.
