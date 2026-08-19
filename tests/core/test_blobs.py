"""Tests for the blob reader (#275).

The two error types carry the whole decision the apply path makes: a scheme this
build cannot read is permanent (a re-issued command's fact would say the same
thing), while a blob that is merely missing may be transient.
"""

import pytest

from src.core.blobs import BlobUnreadable, UnsupportedBlobScheme, read_blob


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

    def test_unsupported_scheme_is_not_caught_as_unreadable(self):
        # The apply path branches on these two types; if one subclassed the
        # other, ordering the excepts wrong would silently restore the loop.
        with pytest.raises(UnsupportedBlobScheme):
            try:
                read_blob("gs://co-temp-blobs/ab/cdef")
            except BlobUnreadable as exc:  # pragma: no cover - must not fire
                pytest.fail(f"unsupported scheme leaked as unreadable: {exc}")
