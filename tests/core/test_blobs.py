"""Tests for the blob reader (#275).

The two error types carry the whole decision the apply path makes: a scheme this
build cannot read is permanent (a re-issued command's fact would say the same
thing), while a blob that is merely missing may be transient.
"""

import pytest

from src.core.blobs import (
    BlobUnreadable,
    UnsupportedBlobScheme,
    aread_blob,
    blob_file,
    read_blob,
)


class TestReadBlob:
    def test_reads_a_file_uri(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert read_blob(f"file://{blob}") == b"<p>hi</p>"

    def test_missing_file_is_unreadable_not_unsupported(self, tmp_path):
        with pytest.raises(BlobUnreadable):
            read_blob(f"file://{tmp_path}/reaped-away.bin")

    def test_unknown_scheme_is_unsupported(self):
        # replicator#7's object-store backend, before this build can read it.
        with pytest.raises(UnsupportedBlobScheme):
            read_blob("gs://co-temp-blobs/ab/cdef")

    def test_missing_uri_is_unreadable(self):
        # A fact that named no blob: nothing about the backend is known, so this
        # is the re-issuable side — and bounded by the caller's cap either way.
        with pytest.raises(BlobUnreadable):
            read_blob(None)

    def test_the_two_error_types_are_siblings(self):
        # The apply path branches on both; if either subclassed the other,
        # ordering the excepts wrong would silently restore the loop (CR-6).
        assert not issubclass(UnsupportedBlobScheme, BlobUnreadable)
        assert not issubclass(BlobUnreadable, UnsupportedBlobScheme)


class TestBlobFile:
    def test_a_local_blob_is_yielded_in_place_and_survives(self, tmp_path):
        # MUST-7: a file:// blob is already a local file, and it is
        # REPLICATOR'S file. Copying it would double every check's disk I/O;
        # deleting it on exit would destroy the fact's own evidence (CR-5).
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        with blob_file(f"file://{blob}") as path:
            assert path == blob
            assert path.read_bytes() == b"<p>hi</p>"

        assert blob.exists()

    def test_a_spooled_copy_is_removed_on_exit(self, tmp_path, monkeypatch):
        # The contract the future non-local arm relies on: whatever this
        # function creates, it cleans up — including when the body raises.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")
        spooled: list = []
        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr(
            "src.core.blobs._SPOOLERS",
            {"file": lambda parsed, fh: fh.write(blob.read_bytes())},
        )

        with pytest.raises(RuntimeError):
            with blob_file(f"file://{blob}") as path:
                spooled.append(path)
                assert path != blob
                assert path.read_bytes() == b"<p>hi</p>"
                raise RuntimeError("the caller blew up mid-read")

        assert spooled and not spooled[0].exists()
        assert blob.exists()

    def test_unsupported_scheme_raises_before_any_temp_file(self):
        with pytest.raises(UnsupportedBlobScheme):
            with blob_file("gs://co-temp-blobs/ab/cdef"):
                pass  # pragma: no cover - never entered


class TestAreadBlob:
    async def test_reads_off_the_event_loop(self, tmp_path):
        # CR-2: the apply task runs in the one load-bearing process, next to the
        # API and the fact consumer. A multi-MB blob read inline stalls both.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert await aread_blob(f"file://{blob}") == b"<p>hi</p>"

    async def test_propagates_the_typed_errors(self, tmp_path):
        with pytest.raises(BlobUnreadable):
            await aread_blob(f"file://{tmp_path}/reaped-away.bin")
        with pytest.raises(UnsupportedBlobScheme):
            await aread_blob("gs://co-temp-blobs/ab/cdef")
