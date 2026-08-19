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

    def test_a_uri_naming_another_host_is_refused(self, tmp_path):
        # CR-9: urlparse drops the netloc and url2pathname returns the bare
        # path, so `file://otherhost/x` used to open the LOCAL /x. On a
        # same-layout host that is a successful read of the wrong bytes, under
        # a real command, with nothing anywhere reporting an error. MUST-7 says
        # host-local; a URI that names another host is not this host's to read.
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        with pytest.raises(BlobUnreadable):
            with blob_file(f"file://otherhost{blob}"):
                pass  # pragma: no cover - never entered

    def test_localhost_and_empty_authority_are_this_host(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"<p>hi</p>")

        assert read_blob(f"file://{blob}") == b"<p>hi</p>"
        assert read_blob(f"file://localhost{blob}") == b"<p>hi</p>"

    def test_a_spooler_raising_anything_becomes_blob_unreadable(self, tmp_path, monkeypatch):
        # CR-10: a backend arm raises what its SDK raises — a 403 from a GCS
        # client is not an OSError. Untyped, it would escape apply_fetch_blob
        # past both excepts, exhaust the retries, and leave the row IN_FLIGHT
        # for the reaper to resurrect forever: the wedge #275 removed.
        def _boom(parsed, fh):
            raise RuntimeError("403 Forbidden")

        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr("src.core.blobs._SPOOLERS", {"file": _boom})

        with pytest.raises(BlobUnreadable):
            with blob_file("file:///tmp/whatever.bin"):
                pass  # pragma: no cover - never entered

    def test_a_spooler_may_still_declare_a_permanent_failure(self, tmp_path, monkeypatch):
        # The wrapper must not swallow the arm's own verdict: an IAM refusal is
        # permanent, and re-fetching three times buys nothing.
        def _forbidden(parsed, fh):
            raise UnsupportedBlobScheme("no objectViewer on the temp bucket")

        monkeypatch.setattr("src.core.blobs.SUPPORTED_SCHEMES", ())
        monkeypatch.setattr("src.core.blobs._SPOOLERS", {"file": _forbidden})

        with pytest.raises(UnsupportedBlobScheme):
            with blob_file("file:///tmp/whatever.bin"):
                pass  # pragma: no cover - never entered

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
