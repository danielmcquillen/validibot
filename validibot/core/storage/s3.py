"""
Amazon S3 storage backend for validation data.

This backend stores files in S3, suitable for:
- AWS deployments
- S3-compatible storage (MinIO, DigitalOcean Spaces, etc.)

Files are stored in a configurable bucket under a configurable prefix
(e.g., s3://bucket/private/runs/...). Download URLs use S3 presigned URLs
for secure, time-limited access.

SECURITY ARCHITECTURE
---------------------
Validibot uses a single bucket with prefix-based access control:

    s3://validibot-storage/
    ├── public/      # Publicly readable (avatars, workflow images)
    └── private/     # Private (submissions, validation data, artifacts)

The bucket should be PRIVATE by default. Public access to the `public/` prefix
can be granted via bucket policy.

STATUS: STUB - Not yet implemented. Install boto3 and complete implementation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from pathlib import Path

from validibot.core.storage.base import DataStorage

logger = logging.getLogger(__name__)


class S3DataStorage(DataStorage):
    """
    Amazon S3 storage backend for validation data.

    Configuration via settings:
        DATA_STORAGE_BUCKET: S3 bucket name
        DATA_STORAGE_PREFIX: Path prefix within bucket (default: "private")
        AWS_REGION: AWS region (optional)
        AWS_ACCESS_KEY_ID: AWS credentials (optional, uses default chain)
        AWS_SECRET_ACCESS_KEY: AWS credentials (optional, uses default chain)

    For S3-compatible storage (MinIO, DigitalOcean Spaces):
        AWS_S3_ENDPOINT_URL: Custom endpoint URL

    STATUS: STUB - Not yet implemented.
    """

    def __init__(
        self,
        bucket_name: str | None = None,
        prefix: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
    ):
        """
        Initialize S3 storage backend.

        Args:
            bucket_name: S3 bucket name. If None, uses settings.DATA_STORAGE_BUCKET
            prefix: Path prefix within bucket. If None, uses DATA_STORAGE_PREFIX
                or "private". All paths will be prefixed with this value.
            region_name: AWS region. If None, uses AWS_REGION or default
            endpoint_url: Custom S3 endpoint for S3-compatible storage
        """
        self.bucket_name = bucket_name or getattr(settings, "DATA_STORAGE_BUCKET", "")
        if not self.bucket_name:
            msg = "DATA_STORAGE_BUCKET setting is required for S3 storage"
            raise ValueError(msg)

        self.prefix = prefix or getattr(settings, "DATA_STORAGE_PREFIX", "private")
        self.region_name = region_name or getattr(settings, "AWS_REGION", None)
        self.endpoint_url = endpoint_url or getattr(
            settings, "AWS_S3_ENDPOINT_URL", None
        )

        # Client initialized lazily
        self._client = None

    def _get_client(self):
        """Get or create boto3 S3 client."""
        if self._client is None:
            try:
                import boto3
            except ImportError as e:
                msg = (
                    "boto3 is required for S3 storage. Install with: pip install boto3"
                )
                raise ImportError(msg) from e

            self._client = boto3.client(
                "s3",
                region_name=self.region_name,
                endpoint_url=self.endpoint_url,
            )
        return self._client

    def _get_object_key(self, path: str) -> str:
        """
        Convert a relative path to a full S3 object key with prefix.

        Args:
            path: Relative path (e.g., "runs/run-123/input/envelope.json")

        Returns:
            Full object key with prefix
            (e.g., "private/runs/run-123/input/envelope.json")
        """
        clean_path = path.lstrip("/")
        if self.prefix:
            return f"{self.prefix.rstrip('/')}/{clean_path}"
        return clean_path

    def write(self, path: str, content: bytes | str) -> str:
        """Write content to S3."""
        raise NotImplementedError(
            "S3DataStorage.write() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def write_file(self, path: str, local_path: Path) -> str:
        """Upload a local file to S3."""
        raise NotImplementedError(
            "S3DataStorage.write_file() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def read(self, path: str) -> bytes:
        """Read content from S3."""
        raise NotImplementedError(
            "S3DataStorage.read() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read text content from S3."""
        raise NotImplementedError(
            "S3DataStorage.read_text() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def download(self, path: str, local_path: Path) -> None:
        """Download a file from S3 to local filesystem."""
        raise NotImplementedError(
            "S3DataStorage.download() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def exists(self, path: str) -> bool:
        """Check if path exists in S3."""
        raise NotImplementedError(
            "S3DataStorage.exists() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def delete(self, path: str) -> None:
        """Delete a file from S3."""
        raise NotImplementedError(
            "S3DataStorage.delete() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def delete_prefix(self, prefix: str) -> int:
        """Delete all files under a prefix."""
        raise NotImplementedError(
            "S3DataStorage.delete_prefix() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def get_uri(self, path: str) -> str:
        """Get s3:// URI for a path."""
        object_key = self._get_object_key(path)
        return f"s3://{self.bucket_name}/{object_key}"

    def get_download_url(
        self,
        path: str,
        *,
        expires_in: int = 3600,
        filename: str | None = None,
    ) -> str:
        """Get a presigned URL for downloading a file."""
        raise NotImplementedError(
            "S3DataStorage.get_download_url() is not yet implemented. "
            "This is a stub for future AWS support."
        )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """
    Parse an S3 URI into bucket name and object key.

    Args:
        uri: S3 URI (e.g., 's3://bucket/path/to/file.json')

    Returns:
        Tuple of (bucket_name, object_key)

    Raises:
        ValueError: If URI format is invalid

    Example:
        >>> bucket, key = parse_s3_uri("s3://my-bucket/private/org/run/input.json")
        >>> bucket
        'my-bucket'
        >>> key
        'private/org/run/input.json'
    """
    if not uri.startswith("s3://"):
        msg = f"Invalid S3 URI (must start with s3://): {uri}"
        raise ValueError(msg)

    # Remove s3:// prefix
    path = uri[5:]

    # Split into bucket and key
    parts = path.split("/", 1)
    if len(parts) != 2:  # noqa: PLR2004
        msg = f"Invalid S3 URI (must be s3://bucket/path): {uri}"
        raise ValueError(msg)

    bucket_name, object_key = parts
    return bucket_name, object_key
