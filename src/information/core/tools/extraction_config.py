"""Translate an InfoSpec ``extraction`` block into HtmlExtractor config.

Shared between Watcher's check pipeline and the Information service's
``preview_extraction`` tool — both need to map the same InfoSpec document
shape onto the extractor's ``selectors`` list.
"""


def extraction_config_from_spec(document: dict) -> dict:
    """Translate an InfoSpec document's ``extraction`` block into HtmlExtractor config."""
    extraction = document.get("extraction") or {}
    algorithm = extraction.get("algorithm", "full_page")
    if algorithm == "css":
        selector = extraction.get("selector", "")
        return {"selectors": [selector]} if selector else {"selectors": []}
    return {"selectors": []}
