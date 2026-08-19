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

**Await ``aread_blob``, never ``read_blob``, from the worker** (CR-2). One
uvicorn process runs the API, the fact consumer and the apply tasks, so a
multi-MB blob read inline stalls all three.

**Non-local backends spool through a temp file** (CR-5). ``blob_file`` yields a
local path and removes anything it created; a ``file://`` blob is Replicator's
own file and is yielded in place, never copied and never deleted. What this
does *not* do is cap memory end to end: co-core's extractor takes ``bytes``, so
one full copy is materialised whatever the backend. The spool keeps a remote
download from being a *second* one.
"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO
from urllib.parse import ParseResult, urlparse
from urllib.request import url2pathname

from src.core.logging import get_logger

logger = get_logger(__name__)

# Schemes whose blobs are already local files (issuer contract MUST-7).
SUPPORTED_SCHEMES = ("file",)

# Authorities a local scheme may carry and still mean "this host". Anything
# else names a machine we are not, and `url2pathname` would silently drop it
# (CR-9) — on a same-layout host, a successful read of the wrong bytes.
LOCAL_NETLOCS = ("", "localhost")

# Scheme → writer that streams the blob onto an open temp file. Empty until
# replicator#7's object store lands; registering an arm here is the whole
# integration point for a new backend. An arm may raise anything its SDK raises
# — ``blob_file`` wraps it as ``BlobUnreadable`` (CR-10) — so an arm only needs
# to raise deliberately when the failure is PERMANENT: raise
# ``UnsupportedBlobScheme`` for a 401/403, which no amount of re-fetching fixes.
_SPOOLERS: dict[str, Callable[[ParseResult, IO[bytes]], None]] = {}


class BlobReadError(Exception):
    """Base: the bytes named by a ``blob_uri`` could not be produced."""


class UnsupportedBlobScheme(BlobReadError):
    """The URI names a backend this build cannot read. Permanent."""


class BlobUnreadable(BlobReadError):
    """The backend is understood; this blob could not be read. May be transient."""


def _parse(blob_uri: str | None) -> ParseResult:
    """The parsed URI, or the typed refusal.

    A missing URI is ``BlobUnreadable`` rather than ``UnsupportedBlobScheme``:
    nothing about the backend is known, so it belongs on the re-issuable side —
    and the caller's cap bounds it either way.
    """
    if not blob_uri:
        raise BlobUnreadable("blob_uri is empty")
    parsed = urlparse(blob_uri)
    if parsed.scheme not in SUPPORTED_SCHEMES and parsed.scheme not in _SPOOLERS:
        raise UnsupportedBlobScheme(f"unsupported blob_uri scheme: {blob_uri!r}")
    return parsed


@contextmanager
def blob_file(blob_uri: str | None) -> Iterator[Path]:
    """A local path holding the blob's bytes, for the duration of the block.

    Cleanup covers exactly what this function created — a spooled copy is
    removed on the way out, including when the body raises; a ``file://`` blob
    is Replicator's, and deleting it would destroy the fact's own evidence.
    """
    parsed = _parse(blob_uri)
    spool = _SPOOLERS.get(parsed.scheme)
    if spool is None:
        if parsed.netloc.lower() not in LOCAL_NETLOCS:
            raise BlobUnreadable(f"blob_uri names another host: {blob_uri!r}")
        yield Path(url2pathname(parsed.path))
        return
    handle = NamedTemporaryFile(prefix="watcher-blob-", delete=False)
    path = Path(handle.name)
    try:
        try:
            with handle:
                spool(parsed, handle)
        except BlobReadError:
            # The arm's own verdict, permanent or not — never reclassified.
            raise
        except Exception as exc:
            raise BlobUnreadable(f"{blob_uri!r}: {exc}") from exc
        yield path
    finally:
        path.unlink(missing_ok=True)


def read_blob(blob_uri: str | None) -> bytes:
    """The bytes at ``blob_uri``. Blocking — see ``aread_blob``."""
    with blob_file(blob_uri) as path:
        try:
            return path.read_bytes()
        except OSError as exc:
            # The URI is in the message, and the caller persists that message to
            # ``fetch_commands.failure_detail`` (CR-4). Fine for file:// and
            # gs://; a credential-bearing URI shape would need redacting here
            # and at the caller's log line before it could be adopted.
            raise BlobUnreadable(f"{blob_uri!r}: {exc}") from exc


async def aread_blob(blob_uri: str | None) -> bytes:
    """``read_blob`` off the event loop — what async callers must use (CR-2)."""
    return await asyncio.to_thread(read_blob, blob_uri)
