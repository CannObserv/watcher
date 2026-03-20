# Phase 2: Content Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the content extraction, chunking, and change detection pipeline — given raw bytes of HTML, PDF, or CSV/Excel content, produce normalized text chunks with hashes and structured change metadata.

**Architecture:** Extractor protocol with three implementations (HTML, PDF, CSV). Each returns an `ExtractionResult` containing chunks (text + hash + excerpt). A `Differ` compares two sets of chunks by hash, producing `ChangeResult` with per-chunk status. SimHash provides fuzzy similarity scoring. New Snapshot/SnapshotChunk/Change models store results in PostgreSQL.

**Tech Stack:** beautifulsoup4 + lxml (HTML), pypdf (PDF), openpyxl (Excel), stdlib csv, custom SimHash (~25 lines)

**Design doc:** `docs/plans/2026-03-20-url-change-monitoring-design.md`

**Issue:** #2

---

## File Structure

```
src/
  core/
    extractors/
      __init__.py        — create: re-export protocol and implementations
      base.py            — create: Extractor protocol, ExtractionResult, Chunk dataclasses
      html.py            — create: HTML extractor (selectors, exclusion, normalization)
      pdf.py             — create: PDF extractor (per-page text)
      csv_excel.py       — create: CSV/Excel extractor (row-range chunking)
    simhash.py           — create: 64-bit SimHash + Hamming distance
    differ.py            — create: chunk-level comparison, ChangeResult
    models/
      snapshot.py        — create: Snapshot, SnapshotChunk models
      change.py          — create: Change model
      __init__.py        — modify: add new model exports
alembic/
  versions/              — new migration for snapshot, snapshot_chunks, changes tables
tests/
  core/
    extractors/
      __init__.py        — create: package
      test_html.py       — create: HTML extractor tests
      test_pdf.py        — create: PDF extractor tests
      test_csv_excel.py  — create: CSV/Excel extractor tests
    test_simhash.py      — create: SimHash tests
    test_differ.py       — create: differ tests
    test_models.py       — modify: add Snapshot/SnapshotChunk/Change model tests
  fixtures/              — create: test fixture files (HTML, PDF, CSV)
```

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add production dependencies**

Add to `pyproject.toml` `dependencies`:
```
"beautifulsoup4>=4.12.0",
"lxml>=5.0",
"pypdf>=5.0",
"openpyxl>=3.1.0",
```

- [ ] **Step 2: Install**

Run: `uv sync`
Expected: resolves and installs without errors

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "#2 chore: add beautifulsoup4, lxml, pypdf, openpyxl dependencies"
```

---

## Task 2: SimHash implementation

**Files:**
- Create: `src/core/simhash.py`
- Test: `tests/core/test_simhash.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_simhash.py`:

```python
"""Tests for SimHash fingerprinting and Hamming distance."""

from src.core.simhash import hamming_distance, simhash


class TestSimHash:
    def test_identical_text_same_hash(self):
        text = "the quick brown fox jumps over the lazy dog"
        assert simhash(text) == simhash(text)

    def test_similar_text_close_hashes(self):
        a = "the quick brown fox jumps over the lazy dog"
        b = "the quick brown fox leaps over the lazy dog"
        dist = hamming_distance(simhash(a), simhash(b))
        assert dist <= 10  # similar texts should have low distance

    def test_different_text_far_hashes(self):
        a = "the quick brown fox jumps over the lazy dog"
        b = "completely unrelated content about quantum physics and mathematics"
        dist = hamming_distance(simhash(a), simhash(b))
        assert dist > 10  # different texts should have high distance

    def test_empty_text_returns_zero(self):
        assert simhash("") == 0

    def test_returns_64_bit_integer(self):
        result = simhash("hello world")
        assert isinstance(result, int)
        assert 0 <= result < (1 << 64)

    def test_whitespace_normalized(self):
        a = "hello   world\n\tfoo"
        b = "hello world foo"
        assert simhash(a) == simhash(b)


class TestHammingDistance:
    def test_identical_is_zero(self):
        assert hamming_distance(0b1010, 0b1010) == 0

    def test_all_bits_different(self):
        assert hamming_distance(0b0000, 0b1111) == 4

    def test_one_bit_different(self):
        assert hamming_distance(0b1000, 0b1001) == 1

    def test_commutative(self):
        a, b = 0xDEADBEEF, 0xCAFEBABE
        assert hamming_distance(a, b) == hamming_distance(b, a)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_simhash.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement SimHash**

Create `src/core/simhash.py`:

```python
"""64-bit SimHash fingerprinting for fuzzy text similarity."""

import hashlib
import re


def simhash(text: str, hashbits: int = 64) -> int:
    """Compute a 64-bit SimHash fingerprint from text.

    Tokenizes on word boundaries after whitespace normalization.
    Returns 0 for empty text.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    tokens = re.findall(r"\w+", normalized.lower())
    if not tokens:
        return 0

    v = [0] * hashbits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(hashbits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1

    fingerprint = 0
    for i in range(hashbits):
        if v[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two integers."""
    return bin(a ^ b).count("1")


def similarity(a: int, b: int, hashbits: int = 64) -> float:
    """Return similarity score between 0.0 (opposite) and 1.0 (identical)."""
    return 1.0 - (hamming_distance(a, b) / hashbits)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_simhash.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/simhash.py tests/core/test_simhash.py
git commit -m "#2 feat: add SimHash fingerprinting and Hamming distance"
```

---

## Task 3: Extractor protocol and data structures

**Files:**
- Create: `src/core/extractors/__init__.py`
- Create: `src/core/extractors/base.py`

- [ ] **Step 1: Write failing test for data structures**

Create `tests/core/extractors/__init__.py` (empty).

Create `tests/core/extractors/test_base.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/extractors/test_base.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement base module**

Create `src/core/extractors/__init__.py`:

```python
"""Content extractors — transform raw bytes into normalized text chunks."""

from src.core.extractors.base import Chunk, ExtractionResult, Extractor

__all__ = ["Chunk", "ExtractionResult", "Extractor"]
```

Create `src/core/extractors/base.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/extractors/test_base.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/extractors/ tests/core/extractors/
git commit -m "#2 feat: add Extractor protocol, Chunk, ExtractionResult data structures"
```

---

## Task 4: HTML extractor

**Files:**
- Create: `src/core/extractors/html.py`
- Create: `tests/fixtures/sample.html`
- Test: `tests/core/extractors/test_html.py`

- [ ] **Step 1: Create test fixture**

Create `tests/fixtures/sample.html`:

```html
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <header><h1>Site Header</h1></header>
  <main>
    <section id="agenda">
      <h2>Meeting Agenda</h2>
      <p>Item 1: Budget review</p>
      <p>Item 2: License applications</p>
    </section>
    <section id="minutes">
      <h2>Previous Minutes</h2>
      <p>Minutes from last meeting approved.</p>
    </section>
  </main>
  <footer>
    <p>Last updated: <span class="timestamp">2026-03-20</span></p>
    <p data-block-id="sq-abc123">Squarespace block</p>
  </footer>
  <script>var x = 1;</script>
  <style>.foo { color: red; }</style>
</body>
</html>
```

- [ ] **Step 2: Write failing tests**

Create `tests/core/extractors/test_html.py`:

```python
"""Tests for HTML content extractor."""

from pathlib import Path

from src.core.extractors.html import HtmlExtractor

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


class TestHtmlExtractor:
    def setup_method(self):
        self.extractor = HtmlExtractor()
        self.html = (FIXTURES / "sample.html").read_bytes()

    def test_extracts_full_page_as_single_chunk(self):
        result = self.extractor.extract(self.html)
        assert len(result.chunks) >= 1
        assert result.chunks[0].chunk_type == "section"

    def test_strips_boilerplate(self):
        result = self.extractor.extract(self.html)
        full_text = " ".join(c.text for c in result.chunks)
        assert "var x = 1" not in full_text
        assert "color: red" not in full_text
        assert "Home" not in full_text  # nav stripped

    def test_selector_targeting(self):
        result = self.extractor.extract(
            self.html, config={"selectors": ["#agenda"]}
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    def test_exclude_selectors(self):
        result = self.extractor.extract(
            self.html, config={"selectors": ["main"], "exclude_selectors": ["#minutes"]}
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    def test_dynamic_id_stripping(self):
        result = self.extractor.extract(
            self.html, config={"dynamic_id_patterns": ["data-block-id"]}
        )
        full_text = " ".join(c.text for c in result.chunks)
        # The attribute should be stripped, content preserved
        assert "sq-abc123" not in full_text
        assert "Squarespace block" in full_text

    def test_whitespace_normalization(self):
        html = b"<html><body><p>  hello   \n\t  world  </p></body></html>"
        result = self.extractor.extract(html)
        text = result.chunks[0].text
        assert "  " not in text
        assert "\n" not in text
        assert "\t" not in text

    def test_sections_create_multiple_chunks(self):
        result = self.extractor.extract(self.html)
        # With boilerplate stripped, main content should produce chunks
        assert len(result.chunks) >= 1
        for chunk in result.chunks:
            assert chunk.label
            assert chunk.text.strip()

    def test_empty_html_returns_empty(self):
        result = self.extractor.extract(b"<html><body></body></html>")
        assert len(result.chunks) == 0 or result.total_chars == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/core/extractors/test_html.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 4: Implement HTML extractor**

Create `src/core/extractors/html.py`:

```python
"""HTML content extractor — selectors, exclusion, normalization."""

import re

from bs4 import BeautifulSoup, Tag

from src.core.extractors.base import Chunk, ExtractionResult

BOILERPLATE_TAGS = {"nav", "footer", "header", "script", "style", "aside", "noscript"}


class HtmlExtractor:
    """Extract normalized text chunks from HTML content."""

    def extract(self, raw: bytes, config: dict | None = None) -> ExtractionResult:
        """Extract text from HTML, applying selectors and normalization.

        Config keys:
            selectors: list[str] — CSS selectors to include (default: whole body)
            exclude_selectors: list[str] — CSS selectors to remove within included content
            dynamic_id_patterns: list[str] — attribute names to strip (e.g. "data-block-id")
            strip_boilerplate: bool — remove nav/footer/script/style (default: True)
        """
        config = config or {}
        soup = BeautifulSoup(raw, "lxml")

        if config.get("strip_boilerplate", True):
            self._strip_boilerplate(soup)

        self._strip_dynamic_ids(soup, config.get("dynamic_id_patterns", []))

        regions = self._select_regions(soup, config)

        if config.get("exclude_selectors"):
            for region in regions:
                for sel in config["exclude_selectors"]:
                    for el in region.select(sel):
                        el.decompose()

        chunks = self._chunk_regions(regions)
        return ExtractionResult(chunks=chunks)

    def _strip_boilerplate(self, soup: BeautifulSoup) -> None:
        """Remove boilerplate elements."""
        for tag in soup.find_all(BOILERPLATE_TAGS):
            tag.decompose()

    def _strip_dynamic_ids(self, soup: BeautifulSoup, patterns: list[str]) -> None:
        """Strip attributes matching dynamic ID patterns and their values from text."""
        for pattern in patterns:
            for tag in soup.find_all(attrs={pattern: True}):
                del tag[pattern]
                # Also remove the attribute value from visible text if it leaked
                if tag.string:
                    tag.string = tag.string

    def _select_regions(
        self, soup: BeautifulSoup, config: dict
    ) -> list[Tag]:
        """Select content regions based on CSS selectors."""
        selectors = config.get("selectors")
        if selectors:
            regions = []
            for sel in selectors:
                regions.extend(soup.select(sel))
            return regions

        body = soup.find("body")
        if body and isinstance(body, Tag):
            return [body]
        return []

    def _chunk_regions(self, regions: list[Tag]) -> list[Chunk]:
        """Split regions into chunks by semantic sections."""
        chunks = []
        index = 0

        for region in regions:
            sections = region.find_all(["section", "article", "div", "main"])
            if sections and any(self._has_direct_text(s) for s in sections):
                for section in sections:
                    text = self._normalize_text(section.get_text(separator=" "))
                    if not text:
                        continue
                    label = self._section_label(section, index)
                    chunks.append(Chunk(
                        index=index, chunk_type="section", label=label, text=text
                    ))
                    index += 1
            else:
                text = self._normalize_text(region.get_text(separator=" "))
                if text:
                    label = self._section_label(region, index)
                    chunks.append(Chunk(
                        index=index, chunk_type="section", label=label, text=text
                    ))
                    index += 1

        return chunks

    def _has_direct_text(self, tag: Tag) -> bool:
        """Check if a tag has meaningful text content."""
        text = tag.get_text(strip=True)
        return len(text) > 0

    def _section_label(self, tag: Tag, index: int) -> str:
        """Generate a label for a section."""
        heading = tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading:
            return heading.get_text(strip=True)[:100]
        tag_id = tag.get("id", "")
        if tag_id:
            return str(tag_id)
        return f"Section {index + 1}"

    def _normalize_text(self, text: str) -> str:
        """Normalize whitespace in extracted text."""
        return re.sub(r"\s+", " ", text).strip()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/extractors/test_html.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add src/core/extractors/html.py tests/core/extractors/test_html.py tests/fixtures/sample.html
git commit -m "#2 feat: add HTML extractor with selectors, exclusion, normalization"
```

---

## Task 5: PDF extractor

**Files:**
- Create: `src/core/extractors/pdf.py`
- Create: `tests/fixtures/sample.pdf`
- Test: `tests/core/extractors/test_pdf.py`

- [ ] **Step 1: Write failing tests**

PDF test fixtures are created in-memory using `pypdf.PdfWriter.add_blank_page()` — no binary fixture files needed.

Create `tests/core/extractors/test_pdf.py`:

```python
"""Tests for PDF content extractor."""

import io

from pypdf import PdfWriter

from src.core.extractors.pdf import PdfExtractor


def _make_pdf_bytes(num_pages: int = 2) -> bytes:
    """Create a minimal PDF with blank pages for testing structure."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestPdfExtractor:
    def setup_method(self):
        self.extractor = PdfExtractor()

    def test_extracts_one_chunk_per_page(self):
        pdf_bytes = _make_pdf_bytes(3)
        result = self.extractor.extract(pdf_bytes)
        assert len(result.chunks) == 3
        for i, chunk in enumerate(result.chunks):
            assert chunk.index == i
            assert chunk.chunk_type == "page"
            assert chunk.label == f"Page {i + 1}"

    def test_empty_pages_excluded(self):
        pdf_bytes = _make_pdf_bytes(2)
        result = self.extractor.extract(pdf_bytes, config={"skip_empty_pages": True})
        # Blank pages have no text, so all should be skipped
        assert len(result.chunks) == 0

    def test_chunk_type_is_page(self):
        pdf_bytes = _make_pdf_bytes(1)
        result = self.extractor.extract(pdf_bytes)
        assert result.chunks[0].chunk_type == "page"

    def test_invalid_pdf_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Failed to parse PDF"):
            self.extractor.extract(b"not a pdf")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/core/extractors/test_pdf.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 4: Implement PDF extractor**

Create `src/core/extractors/pdf.py`:

```python
"""PDF content extractor — per-page text extraction."""

import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.core.extractors.base import Chunk, ExtractionResult


class PdfExtractor:
    """Extract normalized text chunks from PDF content, one chunk per page."""

    def extract(self, raw: bytes, config: dict | None = None) -> ExtractionResult:
        """Extract text from each PDF page.

        Config keys:
            skip_empty_pages: bool — omit pages with no text (default: False)
        """
        config = config or {}
        skip_empty = config.get("skip_empty_pages", False)

        try:
            reader = PdfReader(io.BytesIO(raw))
        except (PdfReadError, Exception) as exc:
            raise ValueError(f"Failed to parse PDF: {exc}") from exc

        chunks = []
        chunk_index = 0
        for page_num, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            text = self._normalize_text(text)

            if skip_empty and not text.strip():
                continue

            chunks.append(Chunk(
                index=chunk_index,
                chunk_type="page",
                label=f"Page {page_num + 1}",
                text=text,
            ))
            chunk_index += 1

        return ExtractionResult(chunks=chunks)

    def _normalize_text(self, text: str) -> str:
        """Normalize whitespace in extracted text."""
        return re.sub(r"\s+", " ", text).strip()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/extractors/test_pdf.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add src/core/extractors/pdf.py tests/core/extractors/test_pdf.py
git commit -m "#2 feat: add PDF extractor with per-page chunking"
```

---

## Task 6: CSV/Excel extractor

**Files:**
- Create: `src/core/extractors/csv_excel.py`
- Test: `tests/core/extractors/test_csv_excel.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/extractors/test_csv_excel.py`:

```python
"""Tests for CSV and Excel content extractor."""

import io

from src.core.extractors.csv_excel import CsvExcelExtractor


def _make_csv(rows: int = 250, cols: int = 3) -> bytes:
    """Create CSV content with header + N data rows."""
    lines = [",".join(f"col{c}" for c in range(cols))]
    for r in range(rows):
        lines.append(",".join(f"val{r}_{c}" for c in range(cols)))
    return "\n".join(lines).encode()


class TestCsvExtractor:
    def setup_method(self):
        self.extractor = CsvExcelExtractor()

    def test_small_csv_single_chunk(self):
        csv_bytes = _make_csv(rows=50)
        result = self.extractor.extract(csv_bytes, config={"content_type": "csv"})
        assert len(result.chunks) == 1
        assert result.chunks[0].chunk_type == "row_range"
        assert result.chunks[0].label == "Rows 1-50"

    def test_large_csv_multiple_chunks(self):
        csv_bytes = _make_csv(rows=250)
        result = self.extractor.extract(csv_bytes, config={"content_type": "csv"})
        assert len(result.chunks) == 3  # 100 + 100 + 50
        assert result.chunks[0].label == "Rows 1-100"
        assert result.chunks[1].label == "Rows 101-200"
        assert result.chunks[2].label == "Rows 201-250"

    def test_chunk_row_size_configurable(self):
        csv_bytes = _make_csv(rows=100)
        result = self.extractor.extract(
            csv_bytes, config={"content_type": "csv", "chunk_rows": 50}
        )
        assert len(result.chunks) == 2

    def test_empty_csv_returns_empty(self):
        result = self.extractor.extract(b"col1,col2\n", config={"content_type": "csv"})
        assert len(result.chunks) == 0

    def test_csv_text_includes_header(self):
        csv_bytes = b"name,age\nAlice,30\nBob,25\n"
        result = self.extractor.extract(csv_bytes, config={"content_type": "csv"})
        text = result.chunks[0].text
        assert "name" in text
        assert "Alice" in text

    def test_sort_normalization(self):
        csv_unsorted = b"name,age\nBob,25\nAlice,30\n"
        csv_sorted = b"name,age\nAlice,30\nBob,25\n"
        r1 = self.extractor.extract(
            csv_unsorted, config={"content_type": "csv", "sort_keys": ["name"]}
        )
        r2 = self.extractor.extract(
            csv_sorted, config={"content_type": "csv", "sort_keys": ["name"]}
        )
        assert r1.chunks[0].content_hash == r2.chunks[0].content_hash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/extractors/test_csv_excel.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement CSV/Excel extractor**

Create `src/core/extractors/csv_excel.py`:

```python
"""CSV and Excel content extractor — row-range chunking."""

import csv
import io

from openpyxl import load_workbook

from src.core.extractors.base import Chunk, ExtractionResult

DEFAULT_CHUNK_ROWS = 100


class CsvExcelExtractor:
    """Extract normalized text chunks from CSV or Excel content."""

    def extract(self, raw: bytes, config: dict | None = None) -> ExtractionResult:
        """Extract text from CSV or Excel, chunked by row ranges.

        Config keys:
            content_type: str — "csv" or "xlsx" (required)
            chunk_rows: int — rows per chunk (default: 100)
            sort_keys: list[str] — column names to sort by before chunking
        """
        config = config or {}
        content_type = config.get("content_type", "csv")
        chunk_size = config.get("chunk_rows", DEFAULT_CHUNK_ROWS)
        sort_keys = config.get("sort_keys")

        if content_type == "xlsx":
            header, rows = self._parse_xlsx(raw)
        else:
            header, rows = self._parse_csv(raw)

        if sort_keys:
            rows = self._sort_rows(rows, header, sort_keys)

        if not rows:
            return ExtractionResult(chunks=[])

        return self._chunk_rows(header, rows, chunk_size)

    def _parse_csv(self, raw: bytes) -> tuple[list[str], list[list[str]]]:
        """Parse CSV bytes into header and data rows."""
        text = raw.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        rows_iter = iter(reader)
        try:
            header = next(rows_iter)
        except StopIteration:
            return [], []
        rows = [row for row in rows_iter if any(cell.strip() for cell in row)]
        return header, rows

    def _parse_xlsx(self, raw: bytes) -> tuple[list[str], list[list[str]]]:
        """Parse Excel bytes into header and data rows."""
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [str(c) if c is not None else "" for c in next(rows_iter)]
        except StopIteration:
            wb.close()
            return [], []
        rows = [
            [str(c) if c is not None else "" for c in row]
            for row in rows_iter
            if any(c is not None for c in row)
        ]
        wb.close()
        return header, rows

    def _sort_rows(
        self, rows: list[list[str]], header: list[str], sort_keys: list[str]
    ) -> list[list[str]]:
        """Sort rows by specified column names."""
        indices = []
        for key in sort_keys:
            if key in header:
                indices.append(header.index(key))
        if not indices:
            return rows
        return sorted(rows, key=lambda row: tuple(row[i] if i < len(row) else "" for i in indices))

    def _chunk_rows(
        self, header: list[str], rows: list[list[str]], chunk_size: int
    ) -> ExtractionResult:
        """Split rows into chunks of chunk_size."""
        chunks = []
        header_line = ",".join(header)

        for i in range(0, len(rows), chunk_size):
            batch = rows[i : i + chunk_size]
            start = i + 1
            end = i + len(batch)
            text_lines = [header_line] + [",".join(row) for row in batch]
            text = "\n".join(text_lines)

            chunks.append(Chunk(
                index=len(chunks),
                chunk_type="row_range",
                label=f"Rows {start}-{end}",
                text=text,
            ))

        return ExtractionResult(chunks=chunks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/extractors/test_csv_excel.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/extractors/csv_excel.py tests/core/extractors/test_csv_excel.py
git commit -m "#2 feat: add CSV/Excel extractor with row-range chunking and sort normalization"
```

---

## Task 7: Differ

**Files:**
- Create: `src/core/differ.py`
- Test: `tests/core/test_differ.py`

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_differ.py`:

```python
"""Tests for chunk-level differ."""

from src.core.differ import ChangeStatus, ChunkChange, diff_chunks
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
        assert result[0].similarity > 0.5  # similar texts

    def test_has_changes_property(self):
        prev = [_chunk(0, "same")]
        curr = [_chunk(0, "same")]
        result = diff_chunks(prev, curr)
        assert not any(c.status != ChangeStatus.UNCHANGED for c in result)

    def test_modified_returns_changed(self):
        prev = [_chunk(0, "old text")]
        curr = [_chunk(0, "new text")]
        result = diff_chunks(prev, curr)
        assert any(c.status != ChangeStatus.UNCHANGED for c in result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_differ.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement differ**

Create `src/core/differ.py`:

```python
"""Chunk-level change detection — compare two sets of chunks by hash."""

import enum
from dataclasses import dataclass

from src.core.extractors.base import Chunk
from src.core.simhash import similarity as simhash_similarity


class ChangeStatus(str, enum.Enum):
    """Status of a chunk comparison."""

    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"


@dataclass
class ChunkChange:
    """Result of comparing a single chunk between snapshots."""

    chunk_index: int
    chunk_label: str
    status: ChangeStatus
    similarity: float | None = None


def diff_chunks(previous: list[Chunk], current: list[Chunk]) -> list[ChunkChange]:
    """Compare two ordered lists of chunks by index and content hash.

    Returns a list of ChunkChange describing each chunk's status.
    """
    prev_by_index = {c.index: c for c in previous}
    curr_by_index = {c.index: c for c in current}

    all_indices = sorted(set(prev_by_index.keys()) | set(curr_by_index.keys()))
    changes = []

    for idx in all_indices:
        prev_chunk = prev_by_index.get(idx)
        curr_chunk = curr_by_index.get(idx)

        if prev_chunk is None and curr_chunk is not None:
            changes.append(ChunkChange(
                chunk_index=idx,
                chunk_label=curr_chunk.label,
                status=ChangeStatus.ADDED,
            ))
        elif prev_chunk is not None and curr_chunk is None:
            changes.append(ChunkChange(
                chunk_index=idx,
                chunk_label=prev_chunk.label,
                status=ChangeStatus.REMOVED,
            ))
        elif prev_chunk is not None and curr_chunk is not None:
            if prev_chunk.content_hash == curr_chunk.content_hash:
                changes.append(ChunkChange(
                    chunk_index=idx,
                    chunk_label=curr_chunk.label,
                    status=ChangeStatus.UNCHANGED,
                ))
            else:
                sim = simhash_similarity(prev_chunk.simhash, curr_chunk.simhash)
                changes.append(ChunkChange(
                    chunk_index=idx,
                    chunk_label=curr_chunk.label,
                    status=ChangeStatus.MODIFIED,
                    similarity=sim,
                ))

    return changes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_differ.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/core/differ.py tests/core/test_differ.py
git commit -m "#2 feat: add chunk-level differ with SimHash similarity scoring"
```

---

## Task 8: Snapshot, SnapshotChunk, and Change models

**Files:**
- Create: `src/core/models/snapshot.py`
- Create: `src/core/models/change.py`
- Modify: `src/core/models/__init__.py`
- Modify: `tests/core/test_models.py`

- [ ] **Step 1: Write failing tests for new models**

Append to `tests/core/test_models.py`:

```python
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.change import Change

# (add these imports at file top)


class TestSnapshotModel:
    def test_create_snapshot(self):
        from ulid import ULID

        snap = Snapshot(
            watch_id=ULID(),
            content_hash="abc123",
            simhash=42,
            storage_path="snapshots/w1/s1.html",
            text_path="snapshots/w1/s1.txt",
            storage_backend="local",
            chunk_count=3,
            text_bytes=1024,
            fetch_duration_ms=200,
            fetcher_used="http",
        )
        assert snap.content_hash == "abc123"
        assert snap.chunk_count == 3
        assert snap.storage_backend == "local"


class TestSnapshotChunkModel:
    def test_create_chunk(self):
        from ulid import ULID

        chunk = SnapshotChunk(
            snapshot_id=ULID(),
            chunk_index=0,
            chunk_type="page",
            chunk_label="Page 1",
            content_hash="def456",
            simhash=99,
            char_count=500,
            excerpt="First 500 chars...",
        )
        assert chunk.chunk_index == 0
        assert chunk.chunk_type == "page"


class TestChangeModel:
    def test_create_change(self):
        from ulid import ULID

        change = Change(
            watch_id=ULID(),
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
            change_metadata={"modified": [{"index": 0, "label": "Page 1"}]},
        )
        assert change.change_metadata["modified"][0]["label"] == "Page 1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_models.py::TestSnapshotModel -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement Snapshot and SnapshotChunk models**

Create `src/core/models/snapshot.py`:

```python
"""Snapshot and SnapshotChunk models — content capture records."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class Snapshot(Base):
    """A single fetch and extraction of a watched URL."""

    __tablename__ = "snapshots"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    simhash: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    text_path: Mapped[str] = mapped_column(Text)
    storage_backend: Mapped[str] = mapped_column(String(20), default="local")
    chunk_count: Mapped[int] = mapped_column(Integer)
    text_bytes: Mapped[int] = mapped_column(BigInteger)
    fetch_duration_ms: Mapped[int] = mapped_column(Integer)
    fetcher_used: Mapped[str] = mapped_column(String(50))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class SnapshotChunk(Base):
    """A structural chunk within a snapshot (page, section, row range)."""

    __tablename__ = "snapshot_chunks"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    snapshot_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("snapshots.id"))
    chunk_index: Mapped[int] = mapped_column(SmallInteger)
    chunk_type: Mapped[str] = mapped_column(String(20))
    chunk_label: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    simhash: Mapped[int] = mapped_column(BigInteger)
    char_count: Mapped[int] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text)
```

- [ ] **Step 4: Implement Change model**

Create `src/core/models/change.py`:

```python
"""Change model — detected differences between consecutive snapshots."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from src.core.models.base import Base, ULIDType, generate_ulid


class Change(Base):
    """A detected change between two snapshots of the same watch."""

    __tablename__ = "changes"

    id: Mapped[ULID] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    watch_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("watches.id"))
    previous_snapshot_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("snapshots.id"))
    current_snapshot_id: Mapped[ULID] = mapped_column(ULIDType, ForeignKey("snapshots.id"))
    change_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __init__(self, **kwargs: object) -> None:
        """Set Python-side defaults for fields not provided."""
        kwargs.setdefault("change_metadata", {})
        super().__init__(**kwargs)
```

- [ ] **Step 5: Update models __init__.py**

Add to `src/core/models/__init__.py`:

```python
from src.core.models.change import Change
from src.core.models.snapshot import Snapshot, SnapshotChunk
```

And add them to `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_models.py -v`
Expected: all model tests pass

- [ ] **Step 7: Commit**

```bash
git add src/core/models/ tests/core/test_models.py
git commit -m "#2 feat: add Snapshot, SnapshotChunk, and Change models"
```

---

## Task 9: Alembic migration for new models

**Files:**
- New: `alembic/versions/<auto>_add_snapshots_chunks_changes.py`

- [ ] **Step 1: Generate migration**

Run:
```bash
export $(cat env | xargs)
uv run alembic revision --autogenerate -m "add snapshots, snapshot_chunks, and changes tables"
```
Expected: detects 3 new tables

- [ ] **Step 2: Fix ULIDType references in migration**

Edit the generated migration to replace any `src.core.models.base.ULIDType(...)` with `sa.String(length=26)` (same pattern as the initial migration).

- [ ] **Step 3: Apply migration**

Run: `uv run alembic upgrade head`
Expected: tables created

- [ ] **Step 4: Verify**

Run: `PGPASSWORD=watcher psql -U watcher -d watcher -c "\dt"`
Expected: `snapshots`, `snapshot_chunks`, `changes` tables listed

- [ ] **Step 5: Commit**

```bash
git add alembic/
git commit -m "#2 feat: add migration for snapshots, snapshot_chunks, and changes tables"
```

---

## Task 10: Update extractors __init__.py and verify full suite

**Files:**
- Modify: `src/core/extractors/__init__.py`

- [ ] **Step 1: Update exports**

Update `src/core/extractors/__init__.py`:

```python
"""Content extractors — transform raw bytes into normalized text chunks."""

from src.core.extractors.base import Chunk, ExtractionResult, Extractor
from src.core.extractors.csv_excel import CsvExcelExtractor
from src.core.extractors.html import HtmlExtractor
from src.core.extractors.pdf import PdfExtractor

__all__ = [
    "Chunk",
    "CsvExcelExtractor",
    "ExtractionResult",
    "Extractor",
    "HtmlExtractor",
    "PdfExtractor",
]
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -v`
Expected: all unit tests pass

- [ ] **Step 3: Run linter**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add src/core/extractors/__init__.py
git commit -m "#2 chore: update extractor exports"
```

---

## Summary

| Task | What it builds | Tests |
|---|---|---|
| 1 | Dependencies | — |
| 2 | SimHash + Hamming distance | ~10 unit |
| 3 | Extractor protocol, Chunk, ExtractionResult | ~6 unit |
| 4 | HTML extractor | ~8 unit |
| 5 | PDF extractor | ~4 unit |
| 6 | CSV/Excel extractor | ~6 unit |
| 7 | Differ | ~9 unit |
| 8 | Snapshot/SnapshotChunk/Change models | ~3 unit |
| 9 | Alembic migration | manual verify |
| 10 | Exports and full suite verification | — |

Total: ~46 new automated tests
