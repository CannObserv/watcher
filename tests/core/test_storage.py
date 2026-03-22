"""Tests for StorageBackend protocol and LocalStorage implementation."""

import importlib
from pathlib import Path

import pytest

import src.core.storage as storage_mod
from src.core.storage import STORAGE_BASE_DIR, LocalStorage


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


class TestStorageBaseDir:
    @pytest.fixture(autouse=True)
    def _reload_teardown(self):
        """Restore storage module state after reload-based tests."""
        yield
        importlib.reload(storage_mod)

    def test_is_path_instance(self):
        assert isinstance(STORAGE_BASE_DIR, Path)

    def test_default_value(self, monkeypatch):
        monkeypatch.delenv("WATCHER_DATA_DIR", raising=False)
        importlib.reload(storage_mod)
        assert storage_mod.STORAGE_BASE_DIR == Path("/var/lib/watcher/data")

    def test_respects_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WATCHER_DATA_DIR", str(tmp_path))
        importlib.reload(storage_mod)
        assert storage_mod.STORAGE_BASE_DIR == tmp_path
