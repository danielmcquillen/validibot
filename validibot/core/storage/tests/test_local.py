"""Tests for LocalDataStorage backend."""

import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

from validibot.core.storage.local import LocalDataStorage


class SampleEnvelope(BaseModel):
    """Sample Pydantic model for testing envelope operations."""

    run_id: str
    status: str
    message: str | None = None


@pytest.fixture
def temp_storage():
    """Create a LocalDataStorage with a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalDataStorage(root=tmpdir)


class TestLocalDataStorage:
    """Tests for LocalDataStorage."""

    def test_write_and_read_bytes(self, temp_storage):
        """Test writing and reading bytes content."""
        content = b"Hello, World!"
        uri = temp_storage.write("test/file.bin", content)

        assert uri.startswith("file://")
        assert temp_storage.exists("test/file.bin")

        result = temp_storage.read("test/file.bin")
        assert result == content

    def test_write_and_read_text(self, temp_storage):
        """Test writing and reading text content."""
        content = "Hello, World!"
        temp_storage.write("test/file.txt", content)

        result = temp_storage.read_text("test/file.txt")
        assert result == content

    def test_write_creates_directories(self, temp_storage):
        """Test that write creates parent directories."""
        temp_storage.write("deep/nested/path/file.txt", "content")

        assert temp_storage.exists("deep/nested/path/file.txt")

    def test_read_nonexistent_raises(self, temp_storage):
        """Test that reading a nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            temp_storage.read("nonexistent.txt")

    def test_exists_returns_false_for_missing(self, temp_storage):
        """Test that exists returns False for missing files."""
        assert not temp_storage.exists("missing.txt")

    def test_delete_removes_file(self, temp_storage):
        """Test that delete removes a file."""
        temp_storage.write("to_delete.txt", "content")
        assert temp_storage.exists("to_delete.txt")

        temp_storage.delete("to_delete.txt")
        assert not temp_storage.exists("to_delete.txt")

    def test_delete_nonexistent_raises(self, temp_storage):
        """Test that deleting a nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            temp_storage.delete("nonexistent.txt")

    def test_get_uri_format(self, temp_storage):
        """Test that get_uri returns proper file:// URI."""
        temp_storage.write("test.txt", "content")
        uri = temp_storage.get_uri("test.txt")

        assert uri.startswith("file://")
        assert "test.txt" in uri

    def test_write_file_from_local_path(self, temp_storage):
        """Test uploading a file from a local path."""
        # Create a temporary file to upload
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(b"file content")
            local_path = Path(f.name)

        try:
            temp_storage.write_file("uploaded.bin", local_path)

            assert temp_storage.exists("uploaded.bin")
            assert temp_storage.read("uploaded.bin") == b"file content"
        finally:
            local_path.unlink()

    def test_write_file_nonexistent_raises(self, temp_storage):
        """Test that write_file raises for nonexistent local file."""
        with pytest.raises(FileNotFoundError):
            temp_storage.write_file("dest.bin", Path("/nonexistent/file"))

    def test_download_to_local_path(self, temp_storage):
        """Test downloading a file to local filesystem."""
        temp_storage.write("source.txt", "download content")

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "downloaded.txt"
            temp_storage.download("source.txt", local_path)

            assert local_path.exists()
            assert local_path.read_text() == "download content"

    def test_download_creates_parent_dirs(self, temp_storage):
        """Test that download creates parent directories."""
        temp_storage.write("source.txt", "content")

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "nested" / "path" / "downloaded.txt"
            temp_storage.download("source.txt", local_path)

            assert local_path.exists()

    def test_write_envelope(self, temp_storage):
        """Test writing a Pydantic envelope."""
        envelope = SampleEnvelope(
            run_id="run-123",
            status="success",
            message="All good",
        )

        temp_storage.write_envelope("run/envelope.json", envelope)

        assert temp_storage.exists("run/envelope.json")
        content = temp_storage.read_text("run/envelope.json")
        assert '"run_id": "run-123"' in content

    def test_read_envelope(self, temp_storage):
        """Test reading and deserializing a Pydantic envelope."""
        envelope = SampleEnvelope(
            run_id="run-456",
            status="pending",
        )
        temp_storage.write_envelope("run/envelope.json", envelope)

        result = temp_storage.read_envelope("run/envelope.json", SampleEnvelope)

        assert isinstance(result, SampleEnvelope)
        assert result.run_id == "run-456"
        assert result.status == "pending"

    def test_path_traversal_prevention(self, temp_storage):
        """Test that path traversal attacks are prevented."""
        with pytest.raises(ValueError, match="Path traversal"):
            temp_storage.write("../../../etc/passwd", "malicious")

        with pytest.raises(ValueError, match="Path traversal"):
            temp_storage.read("../../../etc/passwd")

    def test_get_absolute_path(self, temp_storage):
        """Test getting absolute filesystem path."""
        temp_storage.write("test.txt", "content")
        abs_path = temp_storage.get_absolute_path("test.txt")

        assert isinstance(abs_path, Path)
        assert abs_path.exists()
        assert abs_path.read_text() == "content"

    def test_delete_prefix_removes_directory(self, temp_storage):
        """Test that delete_prefix removes an entire directory tree."""
        # Create a directory structure
        temp_storage.write("runs/run-123/input/envelope.json", '{"test": 1}')
        temp_storage.write("runs/run-123/input/model.idf", "idf content")
        temp_storage.write("runs/run-123/output/envelope.json", '{"result": 2}')
        temp_storage.write("runs/run-456/input/envelope.json", '{"other": 3}')

        # Delete run-123 directory
        count = temp_storage.delete_prefix("runs/run-123/")

        # Should have deleted 3 files
        expected_file_count = 3
        assert count == expected_file_count

        # run-123 files should be gone
        assert not temp_storage.exists("runs/run-123/input/envelope.json")
        assert not temp_storage.exists("runs/run-123/output/envelope.json")

        # run-456 files should still exist
        assert temp_storage.exists("runs/run-456/input/envelope.json")

    def test_delete_prefix_nonexistent_returns_zero(self, temp_storage):
        """Test that delete_prefix returns 0 for nonexistent paths."""
        count = temp_storage.delete_prefix("nonexistent/path/")
        assert count == 0

    def test_delete_prefix_single_file(self, temp_storage):
        """Test that delete_prefix can delete a single file if path matches."""
        temp_storage.write("single_file.txt", "content")

        # Delete the file (without trailing slash it's treated as a file)
        count = temp_storage.delete_prefix("single_file.txt")
        assert count == 1
        assert not temp_storage.exists("single_file.txt")


class TestLocalStorageSignedUrls:
    """Tests for signed URL generation and verification via Django TimestampSigner."""

    def test_generate_download_url(self, temp_storage, settings):
        """Test generating a signed download URL."""
        settings.SECRET_KEY = "test-secret-key"  # noqa: S105
        settings.SITE_URL = "https://example.com"

        temp_storage.write("report.pdf", b"PDF content")

        url = temp_storage.get_download_url("report.pdf", expires_in=3600)

        assert "token=" in url
        assert url.startswith("https://example.com")
        # max_age should NOT appear as a separate query param (it's signed)
        assert "max_age=" not in url

    def test_download_url_nonexistent_raises(self, temp_storage, settings):
        """Test that get_download_url raises for nonexistent files."""
        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        with pytest.raises(FileNotFoundError):
            temp_storage.get_download_url("nonexistent.pdf")

    def test_download_url_includes_filename(self, temp_storage, settings):
        """Test that filename parameter is included in the URL."""
        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        temp_storage.write("report.pdf", b"PDF content")

        url = temp_storage.get_download_url(
            "report.pdf",
            filename="my-report.pdf",
        )

        assert "filename=my-report.pdf" in url

    def test_sign_and_unsign_roundtrip(self, settings):
        """Test that sign_download and unsign_download are symmetric."""
        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        token = LocalDataStorage.sign_download("runs/run-123/output.json", 3600)
        path = LocalDataStorage.unsign_download(token)

        assert path == "runs/run-123/output.json"

    def test_unsign_expired_token_raises(self, settings):
        """Test that expired tokens raise SignatureExpired."""
        from django.core.signing import SignatureExpired

        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        # Sign with max_age=0 so it's expired immediately
        token = LocalDataStorage.sign_download("test.txt", 0)

        with pytest.raises(SignatureExpired):
            LocalDataStorage.unsign_download(token)

    def test_unsign_tampered_token_raises(self, settings):
        """Test that tampered tokens raise BadSignature."""
        from django.core.signing import BadSignature

        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        with pytest.raises(BadSignature):
            LocalDataStorage.unsign_download("tampered:bad-token")

    def test_max_age_cannot_be_extended(self, settings):
        """Test that the client cannot extend the expiry by tampering."""
        from django.core.signing import BadSignature

        settings.SECRET_KEY = "test-secret-key"  # noqa: S105

        # Sign with a short max_age
        token = LocalDataStorage.sign_download("test.txt", 60)
        # Unsign succeeds with the embedded max_age
        assert LocalDataStorage.unsign_download(token) == "test.txt"

        # Tampering with the token payload should fail
        parts = token.rsplit(":", 1)
        tampered = parts[0].replace("|60", "|999999") + ":" + parts[1]
        with pytest.raises(BadSignature):
            LocalDataStorage.unsign_download(tampered)
