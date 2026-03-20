"""Tests for StorageBackend protocol and LocalStorage implementation."""

import pytest

from src.core.storage import LocalStorage


class TestLocalStorage:
    def test_save_and_load(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        content = b"Hello, world!"
        path = "watches/abc/snap1.html"

        storage.save(path, content)
        result = storage.load(path)

        assert result == content

    def test_save_creates_intermediate_directories(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        path = "deep/nested/dir/file.pdf"

        storage.save(path, b"pdf content")

        assert (tmp_path / "deep" / "nested" / "dir" / "file.pdf").exists()

    def test_load_nonexistent_raises(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)

        with pytest.raises(FileNotFoundError):
            storage.load("does/not/exist.txt")

    def test_exists_true(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        storage.save("test.txt", b"data")

        assert storage.exists("test.txt") is True

    def test_exists_false(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)

        assert storage.exists("nope.txt") is False

    def test_build_snapshot_path(self, tmp_path):
        storage = LocalStorage(base_dir=tmp_path)
        path = storage.snapshot_path("watch123", "snap456", "pdf")

        assert path == "snapshots/watch123/snap456.pdf"
