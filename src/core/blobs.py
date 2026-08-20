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

**The ``gs://`` arm** (#275 item 1, replicator#7) reads Replicator's object
store: bucket from the authority, key from the path **verbatim** — the key is
flat (``blobs/<sha256>.bin``), and the ``file://`` shard rule must never be
carried over. Identity comes from ``GCS_BLOB_CREDENTIALS`` (singular BLOB), a
key file for the ``co-gcs-blob-reader`` SA — deliberately NOT
``GOOGLE_APPLICATION_CREDENTIALS``, which names the wheelhouse identity; the
two jobs must not share a principal. The client is built once per process (the
SDK is blocking; a download inside ``asyncio.to_thread`` is beyond cancellation,
so it lands in the shutdown budget). A 404 is the bucket lifecycle rule having
reaped the blob — re-issuable, a fresh fetch repairs it; a 401/403 is a missing
or revoked grant — permanent until an operator acts, and must not burn the cap.
"""

import asyncio
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO
from urllib.parse import ParseResult, urlparse
from urllib.request import url2pathname

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from src.core.logging import get_logger

logger = get_logger(__name__)

GCS_BLOB_CREDENTIALS_ENV = "GCS_BLOB_CREDENTIALS"

# Schemes whose blobs are already local files (issuer contract MUST-7).
SUPPORTED_SCHEMES = ("file",)

# Authorities a local scheme may carry and still mean "this host". Anything
# else names a machine we are not, and `url2pathname` would silently drop it
# (CR-9) — on a same-layout host, a successful read of the wrong bytes.
LOCAL_NETLOCS = ("", "localhost")


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


_gcs_client_cache: storage.Client | None = None
_gcs_client_lock = threading.Lock()


def _gcs_client() -> storage.Client:
    """The process's one GCS client, built lazily from ``GCS_BLOB_CREDENTIALS``.

    Explicitly constructed, never ADC: this process's
    ``GOOGLE_APPLICATION_CREDENTIALS`` is the wheelhouse identity, and handing
    the blob path a package-index principal (or vice versa) is exactly the
    conflation the separate variable exists to prevent.

    An unset variable is ``UnsupportedBlobScheme``: no ``gs://`` blob is
    readable by this process until an operator sets it, so re-issuing would
    spend real origin fetches learning the same thing. Never cached — the next
    call re-reads the environment, so recovery needs no restart of anything
    but the env.
    """
    global _gcs_client_cache
    if _gcs_client_cache is None:
        with _gcs_client_lock:
            if _gcs_client_cache is None:
                key_path = os.environ.get(GCS_BLOB_CREDENTIALS_ENV)
                if not key_path:
                    raise UnsupportedBlobScheme(
                        f"{GCS_BLOB_CREDENTIALS_ENV} is not set — no gs:// blob is readable"
                    )
                _gcs_client_cache = storage.Client.from_service_account_json(key_path)
    return _gcs_client_cache


def _spool_gcs(parsed: ParseResult, fh: IO[bytes]) -> None:
    """Stream one object onto the open temp file.

    Bucket and key come from the URI **verbatim** (replicator#7): the key is
    flat, and deriving anything — the ``file://`` shard rule especially — would
    read the wrong object. Only the two *decided* failures are typed here; the
    ``blob_file`` wrapper turns everything else into ``BlobUnreadable``.
    """
    bucket, key = parsed.netloc, parsed.path.lstrip("/")
    if not bucket or not key:
        raise UnsupportedBlobScheme(f"gs:// blob_uri names no bucket or key: {parsed.geturl()!r}")
    try:
        _gcs_client().bucket(bucket).blob(key).download_to_file(fh)
    except gcs_exceptions.NotFound as exc:
        # The lifecycle rule reaped it (blob_expires_at is a floor, so this can
        # arrive before the fact looks expired). A fresh fetch repairs it.
        raise BlobUnreadable(f"gs object gone: {parsed.geturl()!r}: {exc}") from exc
    except (gcs_exceptions.Forbidden, gcs_exceptions.Unauthorized) as exc:
        # A missing or revoked grant is an operator problem; burning the cap
        # against it spends three origin fetches learning nothing.
        raise UnsupportedBlobScheme(f"gs access refused: {parsed.geturl()!r}: {exc}") from exc


# Scheme → writer that streams the blob onto an open temp file; registering an
# arm here is the whole integration point for a new backend. An arm may raise
# anything its SDK raises — ``blob_file`` wraps it as ``BlobUnreadable``
# (CR-10) — so an arm only needs to raise deliberately when the failure is
# PERMANENT: raise ``UnsupportedBlobScheme`` for a 401/403, which no amount of
# re-fetching fixes.
_SPOOLERS: dict[str, Callable[[ParseResult, IO[bytes]], None]] = {"gs": _spool_gcs}


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
