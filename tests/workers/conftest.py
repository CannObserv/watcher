"""Shared factories for tests/workers/."""

from src.core.info_resolver import ResolvedInfoSpec


def make_resolved(
    *,
    info_item_id: str = "01TESTITEM00000000000000XX",
    info_spec_id: str = "01TESTSPEC00000000000000XX",
    url: str = "https://example.com",
    algorithm: str = "full_page",
    selector: str | None = None,
) -> ResolvedInfoSpec:
    """Build a ResolvedInfoSpec stand-in for tests that drive _run_check_pipeline directly."""
    extraction: dict = {"algorithm": algorithm}
    if selector is not None:
        extraction["selector"] = selector
    document = {
        "schema_version": 1,
        "target": {"url": url},
        "extraction": extraction,
        "fingerprint": {"algorithm": "simhash"},
    }
    return ResolvedInfoSpec(
        info_item_id=info_item_id,
        info_spec_id=info_spec_id,
        document=document,
    )
