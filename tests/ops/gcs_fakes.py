"""Fakes for the object-store client, shared by the backup and restore tests.

Ported from CannObserv/broker's ``tests/gcs_fakes.py`` (broker#4), and held to
its rule: **they owe their fidelity to the SDK, not to the tests.** A fake that
raised where the real client returned ``False`` once let a preflight look tested
while blind to the one misconfiguration it existed for (replicator#7 CR #1). So
every method does what ``google-cloud-storage`` does, and where the real
behaviour is surprising it is commented rather than smoothed over.

The calls this repo makes, and nothing it does not:

- ``Blob.upload_from_filename(..., if_generation_match=0)`` — a create, never a
  put; ``PreconditionFailed`` when the object already exists.
- ``Client.list_blobs(bucket, prefix=, max_results=)`` — lazy; the request
  happens on iteration, and a missing bucket raises ``NotFound`` there.
- ``Bucket.get_blob(name)`` — ``None`` for an absent object, not an exception;
  metadata and the server-set ``time_created`` loaded on the returned blob.
  (Watcher's addition: the backup reads it to tell a re-upload of the same dump
  from a name collision, the restore to check a download against its recorded
  sha256, and a dump's name against the bucket's clock.)
- ``Blob.download_to_file`` — ``NotFound`` for an absent object. Into a handle,
  not a filename: the restore opens it ``O_EXCL`` at 0600 (#296 CR 3).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from google.api_core.exceptions import NotFound, PreconditionFailed


class FakeBlob:
    """Just enough ``google.cloud.storage.Blob`` to answer this repo's calls."""

    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.content_type: str | None = None
        self.size: int | None = None
        # The server's clock, not the writer's: set by the bucket on create.
        self.time_created: datetime | None = None

    def upload_from_filename(
        self,
        filename: str,
        content_type: str | None = None,
        if_generation_match: int | None = None,
        timeout: float | None = None,
    ) -> None:
        if if_generation_match == 0 and self.name in self._bucket.objects:
            # Evaluated against the object's generation before anything is
            # written, which is why an identity holding no
            # ``storage.objects.delete`` still gets a 412 here and not a 403 —
            # observed on broker#4's first real run, not only reasoned.
            raise PreconditionFailed("object already exists")
        self._bucket.objects[self.name] = Path(filename).read_bytes()
        self._bucket.content_types[self.name] = content_type
        # Metadata rides the upload as object metadata, not a second call.
        self._bucket.metadata[self.name] = dict(self.metadata or {})
        self._bucket.created[self.name] = datetime.now(UTC)
        self._bucket.preconditions.append(if_generation_match)

    def download_to_file(self, file_obj: BinaryIO, timeout: float | None = None) -> None:
        """Into a handle the caller opened, so the caller owns its mode and flags."""
        if self.name not in self._bucket.objects:
            raise NotFound("no such object")
        file_obj.write(self._bucket.objects[self.name])


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str | None] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.created: dict[str, datetime] = {}
        self.preconditions: list[int | None] = []

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def get_blob(self, name: str, timeout: float | None = None) -> FakeBlob | None:
        """``None`` when absent — the real ``Bucket.get_blob`` swallows the 404."""
        if name not in self.objects:
            return None
        blob = self.blob(name)
        blob.metadata = dict(self.metadata.get(name, {})) or None
        blob.size = len(self.objects[name])
        blob.time_created = self.created.get(name)
        return blob


class FakeClient:
    """A client over one bucket, with the listing the preflight probes through."""

    def __init__(self, bucket: FakeBucket | None = None, *, missing: bool = False) -> None:
        self._bucket = bucket if bucket is not None else FakeBucket("a-backup-bucket")
        # "This bucket is not there" — the state a misspelled
        # WATCHER_BACKUP_BUCKET puts the job in, and the one the listing
        # preflight exists to report.
        self._missing = missing

    def bucket(self, name: str) -> FakeBucket:
        assert name == self._bucket.name, f"unexpected bucket {name!r}"
        return self._bucket

    def list_blobs(
        self,
        bucket_or_name: str | FakeBucket,
        max_results: int | None = None,
        prefix: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[FakeBlob]:
        """Lazy, like the real one — the request happens when it is iterated."""
        name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        assert name == self._bucket.name, f"unexpected bucket {name!r}"

        def _iter() -> Iterator[FakeBlob]:
            if self._missing:
                raise NotFound(f"bucket {name} not found")
            names = sorted(k for k in self._bucket.objects if not prefix or k.startswith(prefix))
            if max_results is not None:
                names = names[:max_results]
            for key in names:
                # A listing returns full object resources, metadata included.
                yield self._bucket.get_blob(key)

        return _iter()
