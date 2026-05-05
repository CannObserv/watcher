"""Tests for _run_check_pipeline and helpers in workers.pipeline."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType
from src.core.screenshot import ScreenshotResult
from src.core.storage import LocalStorage
from src.workers.pipeline import (
    _EXT_MAP,
    _compute_significance,
    _extract_with_spec,
    _extraction_config_from_spec,
    _run_check_pipeline,
    _to_signed64,
)
from tests.conftest import make_watch
from tests.workers.conftest import make_resolved


class TestToSigned64:
    def test_small_positive_value_unchanged(self):
        assert _to_signed64(42) == 42

    def test_value_at_boundary_unchanged(self):
        boundary = (1 << 63) - 1
        assert _to_signed64(boundary) == boundary

    def test_value_above_boundary_wraps(self):
        val = 1 << 63
        assert _to_signed64(val) == -(1 << 63)

    def test_max_uint64_wraps(self):
        assert _to_signed64((1 << 64) - 1) == -1


class TestExtractorMap:
    def test_ext_map_has_html_only_in_phase2c(self):
        """Phase 2c: only HTML survives the InfoSpec cutover."""
        assert _EXT_MAP == {"html": "html"}


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


@pytest.mark.integration
class TestRunCheckPipeline:
    async def test_first_check_creates_snapshot(self, db_session, tmp_path):
        watch = await make_watch(
            db_session, name="Test", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Hello world</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result["snapshot_id"] is not None
        assert result["is_changed"] is True
        assert result["chunk_count"] >= 1

    async def test_identical_content_no_change(self, db_session, tmp_path):
        watch = await make_watch(
            db_session, name="Stable", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Same content</p></body></html>"

        await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result["is_changed"] is False

    async def test_different_content_detects_change(self, db_session, tmp_path):
        watch = await make_watch(
            db_session, name="Changing", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)

        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result["is_changed"] is True
        assert result["change_id"] is not None

    async def test_stores_raw_content(self, db_session, tmp_path):
        watch = await make_watch(
            db_session, name="Storage", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Stored</p></body></html>"

        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        stored = storage.load(result["storage_path"])
        assert stored == content

    async def test_significance_stored_on_change_record(self, db_session, tmp_path):
        """Persisted Change record has correct significance value."""
        watch = await make_watch(
            db_session, name="Sig", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result2["change_id"] is not None

        stmt = select(Change).where(Change.watch_id == watch.id)
        change = (await db_session.execute(stmt)).scalar_one()
        assert change is not None
        assert change.significance is not None
        assert 0.0 <= change.significance <= 1.0

    async def test_change_persists_info_item_id_and_fingerprints(self, db_session, tmp_path):
        """Change rows carry info_item_id, info_spec_id, and previous/current fingerprints."""
        watch = await make_watch(
            db_session, name="Fp", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        spec = make_resolved(info_item_id=str(watch.info_item_id))
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=spec,
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=spec,
        )
        assert result2["change_id"] is not None
        change = (
            await db_session.execute(select(Change).where(Change.watch_id == watch.id))
        ).scalar_one()
        assert str(change.info_item_id) == str(watch.info_item_id)
        assert str(change.info_spec_id) == spec.info_spec_id
        assert change.previous_fingerprint is not None
        assert change.current_fingerprint is not None

    async def test_change_metadata_includes_significance(self, db_session, tmp_path):
        """change_metadata returned by pipeline includes significance key."""
        watch = await make_watch(
            db_session, name="SigMeta", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result2["change_id"] is not None
        assert "significance" in result2["change_metadata"]
        sig = result2["change_metadata"]["significance"]
        assert 0.0 <= sig <= 1.0

    async def test_change_metadata_includes_change_id(self, db_session, tmp_path):
        """change_metadata returned by pipeline includes change_id key matching change_id result."""
        watch = await make_watch(
            db_session, name="ChgIdMeta", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result2["change_id"] is not None
        assert "change_id" in result2["change_metadata"]
        assert result2["change_metadata"]["change_id"] == result2["change_id"]

    async def test_change_insert_updates_last_changed_at(self, db_session, tmp_path):
        """DB trigger stamps watches.last_changed_at when a Change row is inserted."""
        watch = await make_watch(
            db_session, name="Trigger", url="https://example.com", content_type=ContentType.HTML
        )
        assert watch.last_changed_at is None

        storage = LocalStorage(base_dir=tmp_path)
        await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V1</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        assert result2["change_id"] is not None

        # The trigger fires AFTER INSERT on changes; expire the watch to force a DB re-read.
        await db_session.refresh(watch)
        assert watch.last_changed_at is not None

    async def test_no_change_leaves_last_changed_at_unchanged(self, db_session, tmp_path):
        """last_changed_at stays None when no Change row is ever inserted.

        First pipeline run: establishes baseline snapshot but has no previous to diff against,
        so no Change is created and the trigger never fires.
        Second pipeline run: identical content → fast-path hash match → no new snapshot or Change.
        """
        watch = await make_watch(
            db_session, name="Stable2", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)
        content = b"<html><body><p>Same</p></body></html>"

        await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
            resolved=make_resolved(),
        )
        await db_session.refresh(watch)
        assert watch.last_changed_at is None


class TestModifiedMetadataInvariant:
    """Producer-side invariant: every `modified` item built from differ output
    has truthy `label` (str) and numeric `similarity` ∈ [0, 1].

    The notification body builder relies on this shape — defensive guards in
    `_render_diff_lines` exist only to tolerate minimal-shape test fixtures;
    real producer output should always satisfy the contract.

    Tests at the differ + producer-mapping boundary (decoupled from extractor,
    storage, and DB) so future pipeline edits don't have to set up a full
    end-to-end scenario to catch a shape regression.
    """

    def test_modified_items_have_label_and_similarity(self):
        # Two fingerprints sharing index 1 with different content_hash values
        # guarantee diff_chunks classifies index 1 as MODIFIED (status decided
        # purely by hash equality — no similarity threshold).
        previous = [
            ChunkFingerprint(index=0, label="Stable", content_hash="hA", simhash=0xAAAA),
            ChunkFingerprint(index=1, label="Drift", content_hash="hB", simhash=0xBBBB),
        ]
        current = [
            ChunkFingerprint(index=0, label="Stable", content_hash="hA", simhash=0xAAAA),
            ChunkFingerprint(index=1, label="Drift", content_hash="hC", simhash=0xBBBC),
        ]
        changes = diff_chunks(previous, current)

        # Mirror the exact mapping used in workers/pipeline.py to assemble
        # change_metadata["modified"]. If that mapping ever diverges, this
        # test should be updated alongside it.
        modified = [
            {"label": c.chunk_label, "similarity": c.similarity}
            for c in changes
            if c.status == ChangeStatus.MODIFIED
        ]

        assert modified, "Synthetic input must produce at least one MODIFIED chunk"
        for item in modified:
            assert isinstance(item["label"], str) and item["label"], (
                f"modified item missing/empty label: {item!r}"
            )
            similarity = item["similarity"]
            assert isinstance(similarity, int | float), (
                f"modified item missing similarity: {item!r}"
            )
            assert 0.0 <= float(similarity) <= 1.0, (
                f"modified item similarity out of range: {item!r}"
            )


class TestComputeSignificance:
    def test_all_new_chunks_significance_zero(self):
        """No previous snapshot → all curr chunks are 'added' → 0.0."""
        sig = _compute_significance(added=3, removed=0, modified=0, total_curr=3)
        assert sig == 0.0

    def test_all_removed_clamps_to_zero(self):
        """More removed than curr chunks → clamp to 0.0."""
        sig = _compute_significance(added=0, removed=5, modified=0, total_curr=2)
        assert sig == 0.0

    def test_no_changes_significance_one(self):
        sig = _compute_significance(added=0, removed=0, modified=0, total_curr=5)
        assert sig == 1.0

    def test_mixed_significance(self):
        # 2 changed out of 10 curr → 1 - 2/10 = 0.8
        sig = _compute_significance(added=1, removed=0, modified=1, total_curr=10)
        assert abs(sig - 0.8) < 1e-9

    def test_zero_total_curr_returns_one(self):
        """Edge case: no chunks at all → treat as unchanged."""
        sig = _compute_significance(added=0, removed=0, modified=0, total_curr=0)
        assert sig == 1.0


@pytest.mark.integration
class TestRunCheckPipelineScreenshot:
    async def test_screenshot_saved_when_capture_succeeds(self, db_session, tmp_path):
        """Pipeline sets screenshot_path on snapshot when capture returns bytes."""
        watch = await make_watch(
            db_session, name="Shot", url="https://example.com", content_type=ContentType.HTML
        )

        fake_png = b"\x89PNG\r\nfake"
        fake_result = ScreenshotResult(png_bytes=fake_png, browser="Chromium 130.0.0")
        storage = LocalStorage(base_dir=tmp_path)

        with patch(
            "src.workers.pipeline.capture_screenshot",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hi</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=storage,
                session=db_session,
                resolved=make_resolved(url="https://example.com/screenshot"),
            )

        assert result["screenshot_path"] is not None
        assert result["screenshot_path"].endswith(".png")
        assert storage.exists(result["screenshot_path"])
        stored = storage.load(result["screenshot_path"])
        assert stored == fake_png

        # Snapshot record should reflect path and browser
        snap = (
            await db_session.execute(select(Snapshot).where(Snapshot.watch_id == watch.id))
        ).scalar_one()
        assert snap.screenshot_path == result["screenshot_path"]
        assert snap.screenshot_browser == "Chromium 130.0.0"

    async def test_screenshot_uses_resolved_url(self, db_session, tmp_path):
        """capture_screenshot is invoked with the InfoSpec target URL, not watch.url."""
        watch = await make_watch(
            db_session,
            name="UrlSrc",
            url="https://watch-row.example.com",
            content_type=ContentType.HTML,
        )

        storage = LocalStorage(base_dir=tmp_path)
        capture = AsyncMock(return_value=ScreenshotResult(png_bytes=b"png", browser="x"))
        with patch("src.workers.pipeline.capture_screenshot", new=capture):
            await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hi</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=storage,
                session=db_session,
                resolved=make_resolved(url="https://from-spec.example.com"),
            )

        capture.assert_awaited_once()
        args, _ = capture.call_args
        assert args[0] == "https://from-spec.example.com"

    async def test_screenshot_path_none_when_capture_fails(self, db_session, tmp_path):
        """Pipeline leaves screenshot_path null when capture returns None."""
        watch = await make_watch(
            db_session, name="NoShot", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)

        with patch("src.workers.pipeline.capture_screenshot", new=AsyncMock(return_value=None)):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hi</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=storage,
                session=db_session,
                resolved=make_resolved(),
            )

        assert result["screenshot_path"] is None
        snap = (
            await db_session.execute(select(Snapshot).where(Snapshot.watch_id == watch.id))
        ).scalar_one()
        assert snap.screenshot_path is None

    async def test_pipeline_succeeds_when_screenshot_raises(self, db_session, tmp_path):
        """Screenshot failure never propagates — pipeline still returns a snapshot."""
        watch = await make_watch(
            db_session, name="CrashShot", url="https://example.com", content_type=ContentType.HTML
        )

        storage = LocalStorage(base_dir=tmp_path)

        async def _raise(*_a, **_kw):
            raise RuntimeError("boom")

        with patch("src.workers.pipeline.capture_screenshot", new=_raise):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=b"<html><body><p>Hi</p></body></html>",
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=storage,
                session=db_session,
                resolved=make_resolved(),
            )

        # The pipeline result should still have a snapshot_id
        assert result["snapshot_id"] is not None
