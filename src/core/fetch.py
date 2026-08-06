"""Watcher's User-Agent identity.

This module used to hold ``HttpFetcher``, a thin adapter over co-core's async
fetch effect (#236). The Phase-4 cutover (#241) moved origin fetching out of
Watcher entirely — Replicator performs every request and returns bytes as a
blob — so the adapter, the ``Fetcher`` protocol, and the registry slot that
built it are gone.

What survives is the UA string, and it is still load-bearing for exactly the
reason it always was: fingerprints are UA-sensitive, so the value below travels
out on every ``content.fetch`` command's ``headers`` (see
``src/core/fetch_commands.py``) to keep byte-continuity with revisions captured
before the cutover.
"""

WATCHER_USER_AGENT = "watcher/0.1.0"
