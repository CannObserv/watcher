"""Media-type dispatch helpers (#168 slice 2).

The observed ``content_media_type`` (raw ``Content-Type`` header) drives extractor
selection. These pure helpers derive the lowercased ``type/subtype`` essence, apply
a URL-extension tiebreaker when the header is missing or uninformative, and map an
essence to the CSV/Excel extractor's internal mode.

The Python ``media_type_essence_of`` mirrors the SQL of the
``watched_items.media_type_essence`` generated column
(``WatchedItem.MEDIA_TYPE_ESSENCE_SQL``) — keep the two in sync. The pipeline
derives the dispatch essence in Python (not from the generated column) because the
column is stale on a freshly-seeded, not-yet-flushed WatchedItem.
"""

import os
from urllib.parse import urlparse

# Media types that name no useful extractor — origins commonly mislabel PDFs/CSVs
# as octet-stream or text/plain. When the header essence is one of these (or
# absent), fall back to the URL extension before defaulting to HTML.
AMBIGUOUS_MEDIA_TYPES = frozenset({"application/octet-stream", "binary/octet-stream", "text/plain"})

# URL path suffix -> media-type essence (the cheap tiebreaker; full magic-byte
# sniffing is deferred — see #168).
_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Essence -> the CsvExcelExtractor's internal ``content_type`` config value.
_CSV_EXCEL_SUBTYPE: dict[str, str] = {
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


def media_type_essence_of(raw: str | None) -> str | None:
    """Lowercased ``type/subtype`` with parameters stripped, or None.

    ``"Text/HTML; charset=utf-8"`` -> ``"text/html"``. Mirrors
    ``WatchedItem.MEDIA_TYPE_ESSENCE_SQL``.
    """
    if not raw:
        return None
    essence = raw.split(";", 1)[0].strip().lower()
    return essence or None


def extension_media_type(url: str | None) -> str | None:
    """Map a URL's path extension to a media-type essence, or None."""
    if not url:
        return None
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return _EXTENSION_MEDIA_TYPES.get(ext)


def resolve_dispatch_essence(content_media_type: str | None, url: str | None) -> str | None:
    """Resolve the extractor dispatch essence for a WatchedItem.

    Precedence: the observed/overridden ``content_media_type`` essence when it is
    informative; otherwise the URL-extension tiebreaker; otherwise the (possibly
    None/ambiguous) header essence, which the registry maps to the HTML fallback.
    """
    essence = media_type_essence_of(content_media_type)
    if essence and essence not in AMBIGUOUS_MEDIA_TYPES:
        return essence
    return extension_media_type(url) or essence


def extraction_overrides_for_essence(essence: str | None) -> dict:
    """Extra extraction config implied by the essence.

    The CsvExcelExtractor dispatches internally on ``content_type`` (``csv``/``xlsx``);
    supply it so a spreadsheet isn't parsed as CSV. HTML/PDF need nothing extra.
    """
    subtype = _CSV_EXCEL_SUBTYPE.get(essence or "")
    return {"content_type": subtype} if subtype else {}
