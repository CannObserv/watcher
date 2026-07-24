"""Phase-0 adoption smoke test for the cannobserv substrate (#220).

Proves the ``co-core`` dependency resolves, imports, and is behaviourally
correct against watcher's own code — the end-to-end toolchain check that
Phase 0 exists to validate. ``co_core.pure.util.hashing`` is the chosen
trivial pure util: it sits squarely on watcher's content-fingerprint path
(``Chunk.content_hash`` in ``src/core/extractors/base``), so this doubles as
the Phase-1 anchor — when the shared extractor/fingerprint code moves into
co-core, this parity is the contract it must keep.
"""

import hashlib

from co_core.pure.util.hashing import sha256

from src.core.extractors.base import Chunk


def test_co_core_sha256_matches_stdlib_hexdigest() -> None:
    """co-core's sha256 returns a bare hex digest identical to hashlib's."""
    data = b"cannabis observer"
    assert sha256(data) == hashlib.sha256(data).hexdigest()


def test_co_core_sha256_matches_chunk_content_hash() -> None:
    """co-core's digest equals the ``Chunk.content_hash`` watcher computes today.

    ``Chunk.__post_init__`` sets ``content_hash =
    hashlib.sha256(text.encode()).hexdigest()`` (``src/core/extractors/base``),
    so a future swap to the co-core impl is a no-op on the fingerprint wire.
    """
    text = "Some extracted content.\n"
    chunk = Chunk(index=0, chunk_type="page", label="p1", text=text)
    assert chunk.content_hash == sha256(text.encode("utf-8"))
