"""``WATCHER_EXTRACT_MODE`` — who decides a change (#325, #326).

``local`` — Watcher extracts in-process and decides (the default).
``shadow`` — local still decides; every applied blob is also sent to the
processor, and the comparator judges its answer against local's (#325).
``processor`` — the processor decides: Watcher reads no raw blob, and the
``content.derived`` fact closes the check (#326). Rollback is ``shadow``, then
``local``.

Its own module because two layers read it: the issue/apply paths, and the
conditional-GET key (``src/core/validators.py``), whose extraction generation
is the installed co-core's only while Watcher extracts. It goes with the local
path when the switch's soak ends (#326).
"""

import enum
import os

from src.core.logging import get_logger

logger = get_logger(__name__)

EXTRACT_MODE_ENV = "WATCHER_EXTRACT_MODE"


class ExtractMode(enum.StrEnum):
    """Who decides a change: local extraction (shadowed or not), or the processor."""

    LOCAL = "local"
    SHADOW = "shadow"
    # The design's name was `observo`; Processor replaced Observo (#326).
    PROCESSOR = "processor"


def extract_mode() -> ExtractMode:
    """The configured ``WATCHER_EXTRACT_MODE``; ``local`` when unset.

    An unrecognised value falls back to ``local`` with a warning rather than
    raising: the knob is read on the apply path, and **a knob must not be able
    to wedge the path it governs** (``env_number``'s rule).
    """
    raw = os.environ.get(EXTRACT_MODE_ENV)
    if raw is None:
        return ExtractMode.LOCAL
    try:
        return ExtractMode(raw.strip().lower())
    except ValueError:
        logger.warning(
            "unrecognised %s — local extraction keeps deciding",
            EXTRACT_MODE_ENV,
            extra={"value": raw, "accepted": [m.value for m in ExtractMode]},
        )
        return ExtractMode.LOCAL
