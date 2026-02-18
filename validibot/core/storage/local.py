"""
Local filesystem storage backend for validation data.

This backend stores files on the local filesystem, suitable for:
- Local development (native or Docker)
- Docker Compose deployments using Docker volumes
- Testing

Files are stored under a configurable root directory (DATA_STORAGE_ROOT).
Download URLs use Django's built-in ``TimestampSigner`` to generate
time-limited signed tokens, validated by the ``data_download`` view.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.signing import TimestampSigner
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

    # Separator between path and max_age inside the signed payload.
    # Must not appear in storage paths (paths use '/' separators).
    _SIGNED_SEP = "|"

    def get_download_url(
        self,
        path: str,
        *,
        expires_in: int = 3600,
        filename: str | None = None,
    ) -> str:
        """
        Get a signed URL for downloading a file.

        For local storage, generates a time-limited signed token using
        Django's ``TimestampSigner``. Both the path and expiry window are
        included in the signed payload so neither can be tampered with.

        Args:
            path: Relative path within storage
            expires_in: URL expiry time in seconds (default: 1 hour).
            filename: Optional filename for Content-Disposition header

        Returns:
            URL to the download endpoint with signed token
        """
        full_path = self._resolve_path(path)

        if not full_path.exists():
            msg = f"File does not exist: {path}"
            raise FileNotFoundError(msg)

        token = self.sign_download(path, expires_in)
        base_url = reverse("core:data_download")

        params: dict[str, str] = {"token": token}
        if filename:
            params["filename"] = filename

        url = f"{base_url}?{urlencode(params)}"

        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if site_url:
            url = f"{site_url}{url}"

        return url

    @classmethod
    def sign_download(cls, path: str, max_age: int) -> str:
        """
        Sign a storage path together with its expiry window.

        The signed payload is ``path|max_age`` so the view can enforce
        the original expiry without trusting client-supplied values.
        """
        signer = TimestampSigner()
        return signer.sign(f"{path}{cls._SIGNED_SEP}{max_age}")

    @classmethod
    def unsign_download(cls, token: str) -> str:
        """
        Verify a signed download token and return the original path.

        The ``max_age`` is extracted from the signed payload itself,
        so it cannot be extended by the client.

        Args:
            token: The signed token from :meth:`sign_download`.

        Returns:
            The original storage path.

        Raises:
            ``SignatureExpired`` if the token has expired.
            ``BadSignature`` if the token is invalid or malformed.
        """
        signer = TimestampSigner()

        # First unsign without max_age to extract the payload.
        payload = signer.unsign(token)
        if cls._SIGNED_SEP not in payload:
            from django.core.signing import BadSignature

            msg = "Malformed download token"
            raise BadSignature(msg)

        path, max_age_str = payload.rsplit(cls._SIGNED_SEP, 1)
        try:
            max_age = int(max_age_str)
        except (TypeError, ValueError):
            from django.core.signing import BadSignature

            msg = "Invalid max_age in download token"
            raise BadSignature(msg) from None

        # Now verify with the signed max_age.
        signer.unsign(token, max_age=max_age)
        return path

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
