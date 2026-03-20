"""Tests for CSV and Excel content extractor."""

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
