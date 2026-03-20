"""Tests for extractor base data structures."""

from src.core.extractors.base import Chunk, ExtractionResult


class TestChunk:
    def test_create_chunk(self):
        chunk = Chunk(
            index=0,
            chunk_type="page",
            label="Page 1",
            text="Hello world",
        )
        assert chunk.index == 0
        assert chunk.chunk_type == "page"
        assert chunk.label == "Page 1"
        assert chunk.text == "Hello world"
        assert len(chunk.content_hash) == 64  # SHA-256 hex
        assert chunk.excerpt == "Hello world"
        assert chunk.char_count == 11
        assert chunk.simhash != 0

    def test_chunk_excerpt_truncates(self):
        long_text = "x" * 1000
        chunk = Chunk(index=0, chunk_type="section", label="S1", text=long_text)
        assert len(chunk.excerpt) == 500

    def test_chunk_hash_deterministic(self):
        a = Chunk(index=0, chunk_type="page", label="P1", text="same text")
        b = Chunk(index=0, chunk_type="page", label="P1", text="same text")
        assert a.content_hash == b.content_hash
        assert a.simhash == b.simhash

    def test_chunk_different_text_different_hash(self):
        a = Chunk(index=0, chunk_type="page", label="P1", text="text one")
        b = Chunk(index=0, chunk_type="page", label="P1", text="text two")
        assert a.content_hash != b.content_hash


class TestExtractionResult:
    def test_create_result(self):
        chunks = [
            Chunk(index=0, chunk_type="page", label="P1", text="Hello"),
            Chunk(index=1, chunk_type="page", label="P2", text="World"),
        ]
        result = ExtractionResult(chunks=chunks)
        assert len(result.chunks) == 2
        assert result.total_chars == 10
