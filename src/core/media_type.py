"""Media-type dispatch helpers — re-exported from co-core (#168 slice 2, #324).

The observed ``content_media_type`` (raw ``Content-Type`` header) drives extractor
selection: the lowercased ``type/subtype`` essence, and a URL-extension tiebreaker
when the header is missing or uninformative.

The rule itself lives in ``co_core.pure.extract.media_type`` since cannobserv#486
lifted it byte-for-byte, because two services now run it: watcher resolves the
essence and puts it on the ``content.process`` command, and the processor may
re-resolve it (the tiebreaker needs the origin URL, which only the issuer holds).
That is safe only while both run the *same* function, so this module keeps no
copy — ``tests/core/test_media_type.py`` pins identity, not equal results.

``resolve_dispatch_essence`` is still the single source of truth for the dispatch
essence: the issuer puts it on the command, and ``WatchedItemResponse`` surfaces
its result as the computed ``media_type_essence`` field — there is no
stored/generated column to keep in sync (#168). Stdlib-only on the co-core side,
which is what lets watcher run without ``co-core[extract]`` (#350).
"""

from co_core.pure.extract import (
    AMBIGUOUS_MEDIA_TYPES,
    extension_media_type,
    media_type_essence_of,
    resolve_dispatch_essence,
)

__all__ = [
    "AMBIGUOUS_MEDIA_TYPES",
    "extension_media_type",
    "media_type_essence_of",
    "resolve_dispatch_essence",
]
