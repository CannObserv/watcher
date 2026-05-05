"""Dry-run extraction against a target URL with a candidate InfoSpec document.

Composes ``fetch_and_render`` + ``HtmlExtractor`` + ``extraction_config_from_spec``
+ a per-document fingerprint computation. Returns chunks + total chars + the
computed fingerprint so an authoring agent can verify the spec yields the
expected content before persisting.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from src.core.extractors import HtmlExtractor
from src.core.simhash import simhash
from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    validate_info_spec,
)
from src.information.core.tools.extraction_config import extraction_config_from_spec
from src.information.core.tools.fetch_and_render import HttpFetcherProtocol


@dataclass(frozen=True)
class ChunkPreview:
    """Per-chunk preview for the response — keeps the contract explicit."""

    index: int
    chunk_type: str
    label: str
    text: str
    char_count: int


@dataclass(frozen=True)
class PreviewExtractionResult:
    chunks: list[ChunkPreview]
    total_chars: int
    fingerprint_algorithm: str
    computed_fingerprint: str


class TargetUnreachableError(Exception):
    """Raised when the fetch leg of preview_extraction can't reach the target."""


def _compute_fingerprint(text: str, algorithm: str) -> str:
    """Apply the InfoSpec's fingerprint algorithm to the joined extracted text.

    Mirrors the algorithms accepted by the v1 schema's ``fingerprint.algorithm``
    enum (``sha256`` | ``simhash``). The result is rendered as a hex digest
    (sha256) or decimal int (simhash) — same surface used by Watcher's
    Change rows, just one level higher in the call stack.
    """
    if algorithm == "sha256":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if algorithm == "simhash":
        return str(simhash(text))
    # Schema validation should have rejected anything else; defensive path
    # here is just to surface the unexpected algorithm name.
    raise InfoSpecValidationError(f"Unsupported fingerprint algorithm: {algorithm!r}")


async def preview_extraction(
    fetcher: HttpFetcherProtocol,
    url: str,
    document: dict[str, Any],
) -> PreviewExtractionResult:
    """Validate the spec, fetch ``url``, run extraction, compute the fingerprint.

    Raises ``InfoSpecValidationError`` (route layer translates → 422 with the
    structured error list) and ``TargetUnreachableError`` (route layer →
    422 ``target_unreachable``).
    """
    validate_info_spec(document)  # raises InfoSpecValidationError on failure

    try:
        fetch_result = await fetcher.fetch(url)
    except httpx.HTTPError as e:
        raise TargetUnreachableError(str(e)) from e

    config = extraction_config_from_spec(document)
    extractor = HtmlExtractor()
    extraction = await extractor.extract(fetch_result.content, config=config)

    joined_text = "\n".join(c.text for c in extraction.chunks)
    algorithm = document["fingerprint"]["algorithm"]
    fingerprint = _compute_fingerprint(joined_text, algorithm)

    return PreviewExtractionResult(
        chunks=[
            ChunkPreview(
                index=c.index,
                chunk_type=c.chunk_type,
                label=c.label,
                text=c.text,
                char_count=c.char_count,
            )
            for c in extraction.chunks
        ],
        total_chars=extraction.total_chars,
        fingerprint_algorithm=algorithm,
        computed_fingerprint=fingerprint,
    )
