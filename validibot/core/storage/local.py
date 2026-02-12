"""
Local filesystem storage backend for validation data.

This backend stores files on the local filesystem, suitable for:
- Local development (native or Docker)
- Docker Compose deployments using Docker volumes
- Testing

Files are stored under a configurable root directory (DATA_STORAGE_ROOT).
Download URLs are served through Django's file download endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from django.conf import settings
from django.urls import reverse

from validibot.core.storage.base import DataStorage

logger = logging.getLogger(__name__)


class LocalDataStorage(DataStorage):
    """
    Local filesystem storage for validation data.

    Configuration via settings:
        DATA_STORAGE_ROOT: Base directory for all data files
            Default: BASE_DIR / "data"

    Directory structure mirrors cloud storage paths:
        {DATA_STORAGE_ROOT}/
            runs/
                {org_id}/
                    {run_id}/
                        input.json
                        output.json
                        submission.idf
            validator_assets/
                weather/
                    USA_CA_SF.epw
    """

    def __init__(self, root: Path | str | None = None):
        """
        Initialize local storage backend.

        Args:
            root: Storage root directory. If None, uses settings.DATA_STORAGE_ROOT
                  or falls back to BASE_DIR / "data"
        """
        if root is not None:
            self.root = Path(root)
        elif hasattr(settings, "DATA_STORAGE_ROOT") and settings.DATA_STORAGE_ROOT:
            self.root = Path(settings.DATA_STORAGE_ROOT)
        else:
            self.root = Path(settings.BASE_DIR) / "data"

        # Ensure root directory exists
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, path: str) -> Path:
        """Resolve relative path to absolute filesystem path."""
        # Remove leading slash if present
        clean_path = path.lstrip("/")
        full_path = self.root / clean_path

        # Security: prevent path traversal attacks
        try:
            full_path.resolve().relative_to(self.root.resolve())
        except ValueError:
            msg = f"Path traversal attempt detected: {path}"
            raise ValueError(msg) from None

        return full_path

    def write(self, path: str, content: bytes | str) -> str:
        """Write content to local filesystem."""
        full_path = self._resolve_path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, str):
            full_path.write_text(content, encoding="utf-8")
        else:
            full_path.write_bytes(content)

        logger.debug("Wrote %d bytes to %s", len(content), full_path)
        return self.get_uri(path)

    def write_file(self, path: str, local_path: Path) -> str:
        """Copy a local file to storage."""
        if not local_path.exists():
            msg = f"Local file does not exist: {local_path}"
            raise FileNotFoundError(msg)

        full_path = self._resolve_path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy file content
        content = local_path.read_bytes()
        full_path.write_bytes(content)

        logger.debug("Copied %s to %s", local_path, full_path)
        return self.get_uri(path)

    def read(self, path: str) -> bytes:
        """Read content from local filesystem."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        return full_path.read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read text content from local filesystem."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        return full_path.read_text(encoding=encoding)

    def download(self, path: str, local_path: Path) -> None:
        """Copy a file from storage to local filesystem."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        content = full_path.read_bytes()
        local_path.write_bytes(content)

    def exists(self, path: str) -> bool:
        """Check if path exists in local filesystem."""
        full_path = self._resolve_path(path)
        return full_path.exists()

    def delete(self, path: str) -> None:
        """Delete a file from local filesystem."""
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        full_path.unlink()
        logger.debug("Deleted %s", full_path)

    def delete_prefix(self, prefix: str) -> int:
        """
        Delete all files under a prefix (directory).

        For local storage, this removes the entire directory tree.

        Args:
            prefix: Path prefix to delete (e.g., "runs/run-123/")

        Returns:
            Number of files deleted
        """
        import shutil

        full_path = self._resolve_path(prefix.rstrip("/"))

        if not full_path.exists():
            return 0

        # Count files before deletion
        if full_path.is_file():
            full_path.unlink()
            logger.debug("Deleted file %s", full_path)
            return 1

        # It's a directory - count files and remove tree
        count = sum(1 for _ in full_path.rglob("*") if _.is_file())
        shutil.rmtree(full_path)
        logger.info("Deleted directory %s (%d files)", full_path, count)
        return count

    def ensure_writable(self, path: str) -> None:
        """
        Make a directory writable by non-root container users.

        Validator containers run as UID 1000 but the worker creates
        directories as root. This sets the directory permissions to 777
        so the container can write output files (e.g., output.json).
        """
        import stat

        full_path = self._resolve_path(path)
        if full_path.is_dir():
            full_path.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)

    def get_uri(self, path: str) -> str:
        """Get file:// URI for a path."""
        full_path = self._resolve_path(path)
        return f"file://{full_path}"

    def get_download_url(
        self,
        path: str,
        *,
        expires_in: int = 3600,
        filename: str | None = None,
    ) -> str:
        """
        Get a signed URL for downloading a file.

        For local storage, we generate a signed token that the download
        endpoint validates. This provides similar security to cloud signed URLs.

        Args:
            path: Relative path within storage
            expires_in: URL expiry time in seconds (default: 1 hour)
            filename: Optional filename for Content-Disposition header

        Returns:
            URL to the download endpoint with signed token
        """
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        # Generate signed token
        expires_at = int(time.time()) + expires_in
        token = self._generate_token(path, expires_at)

        # Build URL to download endpoint
        # Note: This URL pattern will be defined in core/urls.py
        try:
            base_url = reverse("core:data_download")
        except Exception:
            # Fallback if URL is not configured yet
            base_url = "/api/v1/data/download/"

        url = f"{base_url}?path={path}&expires={expires_at}&token={token}"
        if filename:
            url += f"&filename={filename}"

        # Prepend site URL if available
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if site_url:
            url = f"{site_url}{url}"

        return url

    def _generate_token(self, path: str, expires_at: int) -> str:
        """
        Generate a signed token for URL authentication.

        Uses HMAC-SHA256 with SECRET_KEY to sign the path and expiry.
        """
        message = f"{path}:{expires_at}"
        signature = hmac.new(
            settings.SECRET_KEY.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return signature

    @staticmethod
    def verify_token(path: str, expires_at: int, token: str) -> bool:
        """
        Verify a signed download token.

        Args:
            path: The file path that was signed
            expires_at: Unix timestamp when token expires
            token: The token to verify

        Returns:
            True if token is valid and not expired, False otherwise
        """
        # Check expiry
        if time.time() > expires_at:
            return False

        # Verify signature
        message = f"{path}:{expires_at}"
        expected = hmac.new(
            settings.SECRET_KEY.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(token, expected)

    def get_absolute_path(self, path: str) -> Path:
        """
        Get the absolute filesystem path for a storage path.

        This is useful when validators need direct filesystem access
        (e.g., when running in the same container as the worker).

        Args:
            path: Relative path within storage

        Returns:
            Absolute filesystem path
        """
        return self._resolve_path(path)
