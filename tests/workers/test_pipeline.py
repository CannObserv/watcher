"""Tests for _run_check_pipeline and helpers in workers.pipeline."""

import pytest
from sqlalchemy import select

from src.core.extractors.base import Chunk
from src.core.models.change import Change
from src.core.models.watch import ContentType, Watch
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
        watch = Watch(name="Test", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

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
        watch = Watch(name="Stable", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

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
        watch = Watch(name="Changing", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

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
        watch = Watch(name="Storage", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

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
        watch = Watch(
            name="Filtered",
            url="https://example.com",
            content_type=ContentType.HTML,
            fetch_config={"ignore_patterns": [r"Noise.*"]},
        )
        db_session.add(watch)
        await db_session.flush()

        storage = LocalStorage(base_dir=tmp_path)
        # First run: establishes baseline
        content_v1 = b"<html><body><p>Signal</p><p>Noise: ignored</p></body></html>"
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
        content_v2 = b"<html><body><p>Signal</p><p>Noise: also ignored</p></body></html>"
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
        watch = Watch(name="Sig", url="https://example.com", content_type=ContentType.HTML)
        db_session.add(watch)
        await db_session.flush()

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


def _make_chunk(text: str, index: int = 0) -> Chunk:
    """Helper to build a Chunk with given text."""
    return Chunk(index=index, chunk_type="text", label=f"chunk-{index}", text=text)


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
