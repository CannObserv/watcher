"""The helpers every bus consume loop shares (#289, #436).

``run_fact_consumer`` (``src/workers/fetch_facts.py``) and
``run_registry_consumer`` (``src/workers/registry_reconcile.py``) both back off
the same way and both log the end of a failure run the same way. One copy, in a
leaf both import: a second copy is how a later change reaches only one of the
loops that needs it, and a leaf keeps the registry loop from importing the
blobs consumer's graph (``fetch_commands``, the Procrastinate blueprint) for a
few lines of shared code.
"""

import asyncio
import logging


async def back_off(stop: asyncio.Event, seconds: float) -> None:
    """Park for ``seconds``, cut short by shutdown.

    Every backing-off handler in both loops calls this, so a change to the
    backoff reaches all of them (#289).
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def log_poll_recovery(log: logging.Logger, topic: str, failed_passes: int) -> int:
    """Log the end of a failure run, if there was one, and return the reset count (#436).

    One INFO ``<topic> poll cycle recovered`` with ``after_failures`` — the
    cohort's grep phrase (CannObserv/replicator#188) — so a broker incident's
    journal shows when each loop came back, not only when it stopped failing.
    It means "this loop is passing again", not "the broker is back": the
    WARNINGs before it carry each failure's cause. ``log`` is the caller's
    logger, so the record names the loop's own module.
    """
    if failed_passes:
        log.info(
            "%s poll cycle recovered",
            topic,
            extra={"topic": topic, "after_failures": failed_passes},
        )
    return 0
