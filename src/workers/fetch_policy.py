"""The stopped ``content.fetch-policy`` producer task (#245; F1, #374 part 1).

Until F1 this task republished every Domain's politeness numbers, plus every
tombstone, every 5 minutes. F1 hands the stream to Archiver. Design D8 allows
one producer, and no ACL refuses a second writer (Watcher's ``+xadd`` grant
outlives this until CannObserv/broker#95), so the rule is held by sequencing:
Watcher's producer stops first, then Archiver's starts.

The task is no longer periodic and nothing defers it. It stays registered only
so a job already queued when the stop deployed drains as a no-op rather than
failing as an unknown task. Part 2 deletes this module with
``src.core.fetch_policy``.
"""

from src.core.logging import get_logger
from src.workers import bp

logger = get_logger(__name__)


@bp.task(name="publish_fetch_policy", queue="default")
async def publish_fetch_policy(**periodic_kwargs) -> dict:
    """Publish nothing: Watcher's fetch-policy producer has stopped (F1)."""
    logger.info("fetch-policy producer stopped (F1)")
    return {"stopped": "F1 (#374)"}
