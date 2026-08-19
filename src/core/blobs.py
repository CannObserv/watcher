"""Reading the bytes a ``content.blobs`` fact points at (#275).

Replicator hands Watcher a claim check — ``blob_uri`` — not the bytes, so
reading it is the one place Watcher parses that URI. The scheme dispatch lives
here rather than in the apply worker: a new backend (replicator#7's object
store) becomes an arm of ``read_blob``, not a second branch in
``apply_fetch_blob``.

The two error types are the point, because the caller's remedy differs:

* ``UnsupportedBlobScheme`` is **deterministic**. A re-issued command's fact
  carries the same scheme, so every re-fetch is a real origin request spent
  learning the same thing — the caller must fail on the first occasion.
* ``BlobUnreadable`` may be transient (the blob reaped between fact and apply,
  a mount blip), so the caller re-issues — under a cap, because a systematic
  cause is indistinguishable from a transient one at this level (#275).

Neither is a subclass of the other: the apply path branches on both, and an
inheritance relationship would make the ``except`` ordering load-bearing.
"""

from urllib.parse import urlparse
from urllib.request import url2pathname

SUPPORTED_SCHEMES = ("file",)


class BlobReadError(Exception):
    """Base: the bytes named by a ``blob_uri`` could not be produced."""


class UnsupportedBlobScheme(BlobReadError):
    """The URI names a backend this build cannot read. Permanent."""


class BlobUnreadable(BlobReadError):
    """The backend is understood; this blob could not be read. May be transient."""


def read_blob(blob_uri: str | None) -> bytes:
    """The bytes at ``blob_uri``.

    A missing URI is ``BlobUnreadable`` rather than ``UnsupportedBlobScheme``:
    nothing about the backend is known, so it belongs on the re-issuable side —
    and the caller's cap bounds it either way.
    """
    if not blob_uri:
        raise BlobUnreadable("blob_uri is empty")
    parsed = urlparse(blob_uri)
    if parsed.scheme not in SUPPORTED_SCHEMES:
        raise UnsupportedBlobScheme(f"unsupported blob_uri scheme: {blob_uri!r}")
    # Host-local by contract (issuer contract MUST-7).
    path = url2pathname(parsed.path)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise BlobUnreadable(f"{blob_uri!r}: {exc}") from exc
