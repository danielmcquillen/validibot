"""Tests for storage registry and factory."""

import tempfile

import pytest

from validibot.core.storage.local import LocalDataStorage
from validibot.core.storage.registry import clear_storage_cache
from validibot.core.storage.registry import get_data_storage
from validibot.core.storage.registry import get_storage_for_uri
from validibot.core.storage.registry import path_from_uri


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear storage cache before and after each test."""
    clear_storage_cache()
    yield
    clear_storage_cache()


class TestGetDataStorage:
    """Tests for get_data_storage factory function."""

    def test_returns_local_by_default(self, settings):
        """Test that local storage is returned by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings.DATA_STORAGE_BACKEND = "local"
            settings.DATA_STORAGE_OPTIONS = {"root": tmpdir}

            storage = get_data_storage()

            assert isinstance(storage, LocalDataStorage)

    def test_caches_instance(self, settings):
        """Test that the storage instance is cached."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings.DATA_STORAGE_BACKEND = "local"
            settings.DATA_STORAGE_OPTIONS = {"root": tmpdir}

            storage1 = get_data_storage()
            storage2 = get_data_storage()

            assert storage1 is storage2

    def test_custom_backend_path(self, settings):
        """Test loading a backend by full class path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings.DATA_STORAGE_BACKEND = (
                "validibot.core.storage.local.LocalDataStorage"
            )
            settings.DATA_STORAGE_OPTIONS = {"root": tmpdir}

            storage = get_data_storage()

            assert isinstance(storage, LocalDataStorage)

    def test_invalid_backend_raises(self, settings):
        """Test that invalid backend raises ImportError."""
        settings.DATA_STORAGE_BACKEND = "nonexistent.module.Storage"
        settings.DATA_STORAGE_OPTIONS = {}

        with pytest.raises(ImportError):
            get_data_storage()

    def test_gcs_backend_requires_bucket(self, settings):
        """Test that GCS backend requires bucket configuration."""
        settings.DATA_STORAGE_BACKEND = "gcs"
        settings.DATA_STORAGE_OPTIONS = {}

        # Should raise because bucket_name is required
        with pytest.raises(RuntimeError):
            get_data_storage()


class TestGetStorageForUri:
    """Tests for get_storage_for_uri function."""

    def test_file_uri_returns_local(self, settings):
        """Test that file:// URIs return LocalDataStorage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings.DATA_STORAGE_ROOT = tmpdir

            storage = get_storage_for_uri(f"file://{tmpdir}/test.txt")

            assert isinstance(storage, LocalDataStorage)

    def test_gs_uri_parses_bucket_name(self):
        """Test that gs:// URIs correctly parse the bucket name."""
        from validibot.core.storage.gcs import parse_gcs_uri

        bucket, path = parse_gcs_uri("gs://my-bucket/path/file.txt")

        assert bucket == "my-bucket"
        assert path == "path/file.txt"

    def test_s3_uri_not_implemented(self):
        """Test that s3:// URIs raise NotImplementedError."""
        with pytest.raises(NotImplementedError):
            get_storage_for_uri("s3://my-bucket/path/file.txt")

    def test_invalid_scheme_raises(self):
        """Test that invalid URI schemes raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported"):
            get_storage_for_uri("ftp://server/file.txt")


class TestPathFromUri:
    """Tests for path_from_uri function."""

    def test_gs_uri(self):
        """Test extracting path from gs:// URI."""
        path = path_from_uri("gs://bucket/runs/org/run/input.json")

        assert path == "runs/org/run/input.json"

    def test_file_uri_with_data_root(self, settings):
        """Test extracting path from file:// URI with DATA_STORAGE_ROOT set."""
        settings.DATA_STORAGE_ROOT = "/app/data"

        path = path_from_uri("file:///app/data/runs/org/run/input.json")

        assert path == "runs/org/run/input.json"

    def test_file_uri_fallback(self, settings):
        """Test extracting path from file:// URI without DATA_STORAGE_ROOT."""
        settings.DATA_STORAGE_ROOT = None

        path = path_from_uri("file:///some/path/data/runs/org/run/input.json")

        # Should find 'data' in path and return everything after
        assert path == "runs/org/run/input.json"

    def test_invalid_uri_format(self):
        """Test that invalid URIs raise ValueError."""
        from validibot.core.storage.base import DataStorage

        with pytest.raises(ValueError, match="missing"):
            DataStorage.parse_uri("not-a-uri")


class TestClearStorageCache:
    """Tests for clear_storage_cache function."""

    def test_clears_cached_instance(self, settings):
        """Test that clear_storage_cache resets the cached instance."""
        with tempfile.TemporaryDirectory() as tmpdir1:
            settings.DATA_STORAGE_BACKEND = "local"
            settings.DATA_STORAGE_OPTIONS = {"root": tmpdir1}

            storage1 = get_data_storage()

            # Clear cache and change settings
            clear_storage_cache()

            with tempfile.TemporaryDirectory() as tmpdir2:
                settings.DATA_STORAGE_OPTIONS = {"root": tmpdir2}

                storage2 = get_data_storage()

                # Should be different instances
                assert storage1 is not storage2
                assert storage1.root != storage2.root
