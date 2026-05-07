"""Consumer-side defaults and translation helpers for InfoSpec consumption.

Watcher applies these when resolving an InfoSpec document into the runtime
arguments needed by ``HtmlExtractor`` and the fetch layer. The same logic is
mirrored in the Archiver service's authoring tools — when changing this module,
mirror the change there.
"""


def extraction_config_from_spec(document: dict) -> dict:
    """Translate an InfoSpec document's ``extraction`` block into HtmlExtractor config."""
    extraction = document.get("extraction") or {}
    algorithm = extraction.get("algorithm", "full_page")
    if algorithm == "css":
        selector = extraction.get("selector", "")
        return {"selectors": [selector]} if selector else {"selectors": []}
    return {"selectors": []}
