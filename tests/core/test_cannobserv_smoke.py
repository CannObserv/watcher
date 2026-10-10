"""Content-acquisition adoption contract for the co-core substrate (#220, #236).

Originally the Phase-0 resolve/import smoke test (#220). The extraction parity
half left with local extraction (#350): the processor computes every
fingerprint now. What stays is the hashing Watcher still does itself — the
change diff checks a stored text against its fingerprint (``diff_loader``).
"""

import hashlib

from co_core.pure.util.hashing import sha256


def test_co_core_sha256_matches_stdlib_hexdigest() -> None:
    """co-core's sha256 returns a bare hex digest identical to hashlib's."""
    data = b"cannabis observer"
    assert sha256(data) == hashlib.sha256(data).hexdigest()
