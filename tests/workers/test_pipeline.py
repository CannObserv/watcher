"""Tests for _run_check_pipeline and helpers in workers.pipeline."""

import pytest

from src.core.models.watch import ContentType, Watch
from src.core.storage import LocalStorage
from src.workers.pipeline import (
    _EXT_MAP,
    _EXTRACTOR_MAP,
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
