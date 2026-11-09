"""WatchedItem helpers shared by the creation path.

The API's lifecycle writers went with ``/api/v1`` (#378): pause and resume,
the item cadence edit and their guards. The ``info.registry`` reconcile is
now the only create path and no operator surface edits an item; the
break-glass domain suspension lives in ``src.core.domains``.
"""

from urllib.parse import urlsplit


def derive_watched_item_name(url: str) -> str:
    """A legible placeholder name derived from a WatchedItem's URL.

    The reconcile, the only creation path since #378, carries no name of its
    own: ``RegistryAnnouncementState`` has no ``name`` field — its grain is
    registry state, not presentation. ``WatchedItem.name`` is NOT NULL, so
    host + path is the most informative thing derivable from what is actually
    in hand, and it is deliberately not invented registry data. Reconciliation
    never overwrites a name already set.
    """
    parts = urlsplit(url)
    name = f"{parts.netloc}{parts.path}".rstrip("/") or url
    return name[:255]
