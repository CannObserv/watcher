"""How long each bus consumer blocks on a read, in one leaf module.

Split out of the worker modules for a dependency-direction reason (#287).
``src.core.bus`` must derive its ``socket_timeout`` from the longest of these
windows — a value at or below one of them manufactures a ``TimeoutError`` on
every idle read against a healthy broker — but importing the workers to read two
integers would put client *construction* downstream of the loops that consume
its client.

That is a cycle, not a style preference:
``src.workers.registry_reconcile`` imports ``src.workers.watch_status``, which
imports ``src.core.bus``. A leaf both sides import cannot cycle, and an
``ImportError`` at startup is a poor way to discover a layering mistake.

The worker modules re-export these as their own ``BLOCK_MS`` so their call sites
and defaults read unchanged, and so the discovery guard in
``tests/core/test_bus_client_policy.py`` still finds them where the loops are.
"""

# The content.blobs fact consumer (src/workers/fetch_facts.py).
BLOBS_BLOCK_MS = 5_000

# The groupless info.registry tail (src/workers/registry_reconcile.py). Equal to
# the blobs window today and owned separately on purpose: they answer to
# different producers and there is no reason they must move together.
REGISTRY_BLOCK_MS = 5_000

# What src.core.bus derives its socket timeout from. Declared here rather than
# computed at the client, so the client depends on one leaf name instead of on
# every module that happens to block. The claim that this really is the longest
# is audited against a discovery walk over src.workers in the tests.
LONGEST_BLOCK_MS = max(BLOBS_BLOCK_MS, REGISTRY_BLOCK_MS)
