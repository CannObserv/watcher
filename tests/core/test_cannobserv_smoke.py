"""Content-acquisition adoption contract for the co-core substrate (#220, #236).

Originally the Phase-0 resolve/import smoke test (#220), anchored on the
fingerprint path. #236 completed the swap — the shared extractor/fingerprint
code now lives in co-core and the ``src/core`` mirror is gone — so this file is
the parity contract that swap must keep: co-core's sha256 util matches stdlib,
and the pipeline's fingerprint over co-core's ``HtmlExtractor`` output is stable
against a pinned golden (guards silent extraction/algorithm drift across
co-core upgrades that would spuriously re-fingerprint the whole watch set).
"""

import hashlib

from co_core.pure.extract.html import HtmlExtractor
from co_core.pure.util.hashing import sha256

# Representative HTML exercising a heading, a paragraph, and list items.
_SAMPLE = (
    b"<html><head><title>Licenses</title></head><body>"
    b"<h1>License 42</h1><p>Status: active</p>"
    b"<ul><li>alpha</li><li>beta</li></ul>"
    b"</body></html>"
)
# Pinned fingerprint under the pipeline formula (see src/workers/pipeline.py:
# `"sha256:" + sha256("\n".join(c.text for c in chunks))`). A change here means
# co-core's extraction or hashing shifted — investigate before re-pinning.
_GOLDEN_FINGERPRINT = "sha256:c71573ab273edd866d1624f01efc65f7b4f17c9092efabac4f3207c0f7c4a191"


def test_co_core_sha256_matches_stdlib_hexdigest() -> None:
    """co-core's sha256 returns a bare hex digest identical to hashlib's."""
    data = b"cannabis observer"
    assert sha256(data) == hashlib.sha256(data).hexdigest()


def test_pipeline_fingerprint_over_co_core_extraction_is_stable() -> None:
    """The watcher fingerprint over co-core's HtmlExtractor output is unchanged.

    Reproduces the pipeline's fingerprint formula so a co-core extraction or
    hashing change surfaces here as a red test rather than as a silent
    one-time "change detected" across every watched item at cutover.
    """
    result = HtmlExtractor().extract(_SAMPLE, config={"selectors": []})
    content_bytes = "\n".join(c.text for c in result.chunks).encode()
    fingerprint = "sha256:" + hashlib.sha256(content_bytes).hexdigest()
    assert fingerprint == _GOLDEN_FINGERPRINT
