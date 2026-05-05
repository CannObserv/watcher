"""Tests for _run_check_pipeline and helpers in workers.pipeline."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.core.differ import ChangeStatus, ChunkFingerprint, diff_chunks
from src.core.extractors.base import Chunk
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.screenshot import ScreenshotResult
from src.core.storage import LocalStorage
from src.workers.pipeline import (
    _EXT_MAP,
    _EXTRACTOR_MAP,
    _apply_ignore_patterns,
    _compute_significance,
    _extract_content,
    _run_check_pipeline,
    _to_signed64,
)
from tests.conftest import make_watch


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
    def test_ext_map_has_known_keys(self):
        assert "html" in _EXT_MAP
        assert "pdf" in _EXT_MAP
        assert "file" in _EXT_MAP

    def test_extractor_map_matches_ext_map_keys(self):
        assert set(_EXTRACTOR_MAP.keys()) == set(_EXT_MAP.keys())


@pytest.mark.skip(
    reason="Phase 2c — _extract_content rewires onto InfoSpec extraction in Task 7 (#138)."
)
class TestExtractContent:
    async def test_extracts_html_content(self):
        watch = Watch(name="Test", url="https://example.com", content_type=ContentType.HTML)
        result = await _extract_content(watch, b"<html><body><p>Hello</p></body></html>")
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    async def test_extracts_file_content(self):
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", "value"])
        writer.writerow(["foo", "1"])
        csv_bytes = buf.getvalue().encode()

        watch = Watch(
            name="CSV",
            url="https://example.com/data.csv",
            content_type=ContentType.FILE,
            fetch_config={"file_format": "csv"},
        )
        result = await _extract_content(watch, csv_bytes)
        assert len(result.chunks) >= 1


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
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
        )
        result = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
        )
        stored = storage.load(result["storage_path"])
        assert stored == content

    async def test_ignore_patterns_filter_chunks_in_pipeline(self, db_session, tmp_path):
        """Chunks matching ignore_patterns are excluded from diff and snapshot."""
        watch = await make_watch(
            db_session,
            name="Filtered",
            url="https://example.com",
            content_type=ContentType.HTML,
            fetch_config={"ignore_patterns": [r"Noise.*"]},
        )

        storage = LocalStorage(base_dir=tmp_path)
        # First run: establishes baseline
        # Use <section> tags so the HTML extractor produces separate chunks
        # (without them, the body text becomes a single chunk and fullmatch fails).
        content_v1 = (
            b"<html><body><section>Signal</section><section>Noise: ignored</section></body></html>"
        )
        result1 = await _run_check_pipeline(
            watch=watch,
            raw_content=content_v1,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        # chunk_count reflects filtered chunks only
        assert result1["chunk_count"] >= 1

        # Second run: only the noisy chunk changes; signal is stable
        content_v2 = (
            b"<html><body>"
            b"<section>Signal</section>"
            b"<section>Noise: also ignored</section>"
            b"</body></html>"
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=content_v2,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        # Both runs produce a new snapshot (different raw hash), but since the
        # matching chunk is filtered, no Change record should be created.
        assert result2["change_id"] is None

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
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        assert result2["change_id"] is not None

        stmt = select(Change).where(Change.watch_id == watch.id)
        change = (await db_session.execute(stmt)).scalar_one()
        assert change is not None
        assert change.significance is not None
        assert 0.0 <= change.significance <= 1.0

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
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
        )
        result2 = await _run_check_pipeline(
            watch=watch,
            raw_content=b"<html><body><p>V2 changed</p></body></html>",
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
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
        )
        await _run_check_pipeline(
            watch=watch,
            raw_content=content,
            fetcher_used="http",
            fetch_duration_ms=100,
            storage=storage,
            session=db_session,
        )
        await db_session.refresh(watch)
        assert watch.last_changed_at is None


def _make_chunk(text: str, index: int = 0) -> Chunk:
    """Helper to build a Chunk with given text."""
    return Chunk(index=index, chunk_type="text", label=f"chunk-{index}", text=text)


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


class TestApplyIgnorePatterns:
    def test_matching_pattern_excludes_chunk(self):
        chunks = [_make_chunk("foo bar"), _make_chunk("keep me", index=1)]
        result = _apply_ignore_patterns(chunks, [r"foo bar"])
        assert len(result) == 1
        assert result[0].text == "keep me"

    def test_non_matching_pattern_preserves_chunk(self):
        chunks = [_make_chunk("hello world")]
        result = _apply_ignore_patterns(chunks, [r"something else"])
        assert len(result) == 1
        assert result[0].text == "hello world"

    def test_empty_patterns_returns_all_chunks(self):
        chunks = [_make_chunk("a"), _make_chunk("b", index=1)]
        result = _apply_ignore_patterns(chunks, [])
        assert result == chunks

    def test_partial_match_does_not_exclude(self):
        """fullmatch required — partial regex hit must not drop the chunk."""
        chunks = [_make_chunk("foo bar baz")]
        result = _apply_ignore_patterns(chunks, [r"foo bar"])
        assert len(result) == 1

    def test_regex_pattern_matches(self):
        chunks = [_make_chunk("2024-01-15"), _make_chunk("keep", index=1)]
        result = _apply_ignore_patterns(chunks, [r"\d{4}-\d{2}-\d{2}"])
        assert len(result) == 1
        assert result[0].text == "keep"


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
            )

        # The pipeline result should still have a snapshot_id
        assert result["snapshot_id"] is not None

    @pytest.mark.parametrize("content_type", [ContentType.PDF, ContentType.FILE])
    async def test_screenshot_skipped_for_non_html(self, db_session, tmp_path, content_type):
        """Screenshot step is skipped entirely for non-HTML content types."""
        import csv
        import io

        if content_type == ContentType.FILE:
            buf = io.StringIO()
            csv.writer(buf).writerows([["col"], ["val"]])
            raw = buf.getvalue().encode()
            fetch_config = {"file_format": "csv"}
        else:
            # Minimal valid single-page PDF
            raw = (
                b"%PDF-1.4\n"
                b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n"
                b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n"
                b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>>\nendobj\n"
                b"xref\n0 4\n"
                b"0000000000 65535 f \n"
                b"0000000009 00000 n \n"
                b"0000000058 00000 n \n"
                b"0000000115 00000 n \n"
                b"trailer\n<</Size 4 /Root 1 0 R>>\n"
                b"startxref\n190\n%%EOF\n"
            )
            fetch_config = None

        watch = await make_watch(
            db_session,
            name="NonHTML",
            url="https://example.com/file",
            content_type=content_type,
            fetch_config=fetch_config,
        )

        storage = LocalStorage(base_dir=tmp_path)
        mock_capture = AsyncMock(return_value=ScreenshotResult(png_bytes=b"fakepng", browser="x"))

        with patch("src.workers.pipeline.capture_screenshot", new=mock_capture):
            result = await _run_check_pipeline(
                watch=watch,
                raw_content=raw,
                fetcher_used="http",
                fetch_duration_ms=100,
                storage=storage,
                session=db_session,
            )

        mock_capture.assert_not_called()
        assert result["screenshot_path"] is None
