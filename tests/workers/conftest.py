"""Shared factories for tests/workers/."""

from src.core.sources.resolver import ResolvedRootSource


def make_resolved(
    *,
    info_source_id: str = "01TESTSOURCE000000000000XX",
    url: str = "https://example.com",
    algorithm: str = "full_page",
    selector: str | None = None,
) -> ResolvedRootSource:
    """Build a ResolvedRootSource stand-in for tests that drive _run_check_pipeline directly."""
    extraction: dict = {"algorithm": algorithm}
    if selector is not None:
        extraction["selector"] = selector
    source_spec = {
        "schema_version": 1,
        "target": {"url": url},
        "extraction": extraction,
        "fingerprint": {"algorithm": "simhash"},
    }
    return ResolvedRootSource(
        info_source_id=info_source_id,
        url=url,
        source_spec=source_spec,
        children=[],
    )
