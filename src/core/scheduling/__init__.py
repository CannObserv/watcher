"""Schedule resolution for WatchedItems (post-#191 single-entity model).

The per-Watch creation service (`create_watch`) and the override resolution
chain were removed when `Watch` was folded into `WatchedItem`. See
``resolution.py`` for the surviving schedule resolver.
"""
