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

    def _select_regions(self, soup: BeautifulSoup, config: dict) -> list[Tag]:
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
        """Split regions into chunks by semantic sections.

        Uses direct children only to avoid duplicate text from nested elements.
        Falls back to the region itself if no semantic children are found.
        """
        chunks = []
        index = 0

        for region in regions:
            sections = region.find_all(
                ["section", "article"], recursive=False
            )
            if sections:
                for section in sections:
                    text = self._normalize_text(section.get_text(separator=" "))
                    if not text:
                        continue
                    label = self._section_label(section, index)
                    chunks.append(Chunk(
                        index=index, chunk_type="section", label=label, text=text,
                    ))
                    index += 1
            else:
                text = self._normalize_text(region.get_text(separator=" "))
                if text:
                    label = self._section_label(region, index)
                    chunks.append(Chunk(
                        index=index, chunk_type="section", label=label, text=text,
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
