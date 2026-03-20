"""Extractor protocol and shared data structures."""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from src.core.simhash import simhash as compute_simhash

EXCERPT_MAX_LENGTH = 500


@dataclass
class Chunk:
    """A structural unit of extracted content (page, section, row range)."""

    index: int
    chunk_type: str
    label: str
    text: str
    content_hash: str = field(init=False)
    simhash: int = field(init=False)
    char_count: int = field(init=False)
    excerpt: str = field(init=False)

    def __post_init__(self) -> None:
        self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()
        self.simhash = compute_simhash(self.text)
        self.char_count = len(self.text)
        self.excerpt = self.text[:EXCERPT_MAX_LENGTH]


@dataclass
class ExtractionResult:
    """Output of an extractor — a list of chunks."""

    chunks: list[Chunk]

    @property
    def total_chars(self) -> int:
        """Total character count across all chunks."""
        return sum(c.char_count for c in self.chunks)


class Extractor(Protocol):
    """Protocol for content extractors."""

    def extract(self, raw: bytes, config: dict | None = None) -> ExtractionResult:
        """Extract text chunks from raw content bytes."""
        ...
