"""Tests for chunk-level differ."""

from src.core.differ import ChangeStatus, diff_chunks
from src.core.extractors.base import Chunk


def _chunk(index: int, text: str, label: str = "") -> Chunk:
    """Helper to create a chunk with minimal args."""
    return Chunk(index=index, chunk_type="page", label=label or f"P{index}", text=text)


class TestDiffChunks:
    def test_no_changes(self):
        prev = [_chunk(0, "hello"), _chunk(1, "world")]
        curr = [_chunk(0, "hello"), _chunk(1, "world")]
        result = diff_chunks(prev, curr)
        assert len(result) == 2
        assert all(c.status == ChangeStatus.UNCHANGED for c in result)

    def test_modified_chunk(self):
        prev = [_chunk(0, "hello world")]
        curr = [_chunk(0, "hello earth")]
        result = diff_chunks(prev, curr)
        assert len(result) == 1
        assert result[0].status == ChangeStatus.MODIFIED
        assert 0.0 < result[0].similarity < 1.0

    def test_added_chunk(self):
        prev = [_chunk(0, "page one")]
        curr = [_chunk(0, "page one"), _chunk(1, "page two")]
        result = diff_chunks(prev, curr)
        added = [c for c in result if c.status == ChangeStatus.ADDED]
        assert len(added) == 1
        assert added[0].chunk_index == 1

    def test_removed_chunk(self):
        prev = [_chunk(0, "page one"), _chunk(1, "page two")]
        curr = [_chunk(0, "page one")]
        result = diff_chunks(prev, curr)
        removed = [c for c in result if c.status == ChangeStatus.REMOVED]
        assert len(removed) == 1
        assert removed[0].chunk_index == 1

    def test_all_new(self):
        result = diff_chunks([], [_chunk(0, "new")])
        assert len(result) == 1
        assert result[0].status == ChangeStatus.ADDED

    def test_all_removed(self):
        result = diff_chunks([_chunk(0, "old")], [])
        assert len(result) == 1
        assert result[0].status == ChangeStatus.REMOVED

    def test_similarity_score_for_modified(self):
        prev = [_chunk(0, "the quick brown fox jumps over the lazy dog")]
        curr = [_chunk(0, "the quick brown fox leaps over the lazy dog")]
        result = diff_chunks(prev, curr)
        assert result[0].status == ChangeStatus.MODIFIED
        assert result[0].similarity > 0.5

    def test_unchanged_no_changes(self):
        prev = [_chunk(0, "same")]
        curr = [_chunk(0, "same")]
        result = diff_chunks(prev, curr)
        assert not any(c.status != ChangeStatus.UNCHANGED for c in result)

    def test_modified_returns_changed(self):
        prev = [_chunk(0, "old text")]
        curr = [_chunk(0, "new text")]
        result = diff_chunks(prev, curr)
        assert any(c.status != ChangeStatus.UNCHANGED for c in result)
