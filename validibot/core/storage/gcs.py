"""
Google Cloud Storage backend for validation data.

This backend stores files in GCS, suitable for:
- Production GCP deployments
- Staging/testing with GCS

Files are stored in a configurable bucket under a configurable prefix
(e.g., gs://bucket/private/runs/...). Download URLs use GCS signed URLs
for secure, time-limited access.

SECURITY ARCHITECTURE
---------------------
Validibot uses a single bucket with prefix-based access control:

    gs://validibot-storage/
    ├── public/      # Publicly readable (avatars, workflow images)
    └── private/     # Private (submissions, validation data, artifacts)

The bucket is PRIVATE by default. Public access to the `public/` prefix
is granted via IAM Conditions:

    gcloud storage buckets add-iam-policy-binding gs://BUCKET \\
        --member="allUsers" \\
        --role="roles/storage.objectViewer" \\
        --condition='expression=resource.name.startsWith("projects/_/buckets/BUCKET/objects/public/"),title=public-prefix-only'

The `private/` prefix remains accessible only to the service account.
Users access private files via time-limited signed URLs.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path  # noqa: TC003 - Used at runtime in type annotations
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.core.storage.base import DataStorage

if TYPE_CHECKING:
    from google.cloud.storage import Bucket
    from google.cloud.storage import Client

logger = logging.getLogger(__name__)


class GCSDataStorage(DataStorage):
    """
    Google Cloud Storage backend for validation data.

    Configuration via settings:
        DATA_STORAGE_BUCKET: GCS bucket name
        DATA_STORAGE_PREFIX: Path prefix within bucket (default: "private")
        GCP_PROJECT_ID: Google Cloud project ID (optional, uses default)

    The bucket should be configured with:
    - Private access at bucket level (no public access)
    - IAM Condition granting allUsers objectViewer on public/* prefix
    - Appropriate lifecycle rules for cleanup
    - The service account needs storage.objects.create/get/delete on private/*

    See docs/dev_docs/how-to/configure-storage.md for setup instructions.
    """

    def __init__(
        self,
        bucket_name: str | None = None,
        prefix: str | None = None,
        project_id: str | None = None,
    ):
        """
        Initialize GCS storage backend.

        Args:
            bucket_name: GCS bucket name. If None, uses settings.DATA_STORAGE_BUCKET
            prefix: Path prefix within bucket. If None, uses DATA_STORAGE_PREFIX
                or "private". All paths will be prefixed with this value.
            project_id: GCP project ID. If None, uses GCP_PROJECT_ID or default
        """
        # Lazy import to avoid requiring google-cloud-storage for local dev
        from google.cloud import storage

        self.bucket_name = bucket_name or getattr(settings, "DATA_STORAGE_BUCKET", "")
        if not self.bucket_name:
            msg = "DATA_STORAGE_BUCKET setting is required for GCS storage"
            raise ValueError(msg)

        # Prefix for all paths (e.g., "private" -> all files under private/)
        self.prefix = prefix or getattr(settings, "DATA_STORAGE_PREFIX", "private")

        self.project_id = project_id or getattr(settings, "GCP_PROJECT_ID", None)

        # Initialize client lazily
        self._client: Client | None = None
        self._bucket: Bucket | None = None
        self._storage_module = storage

    @property
    def client(self) -> Client:
        """Get or create GCS client."""
        if self._client is None:
            self._client = self._storage_module.Client(project=self.project_id)
        return self._client

    @property
    def bucket(self) -> Bucket:
        """Get or create bucket reference."""
        if self._bucket is None:
            self._bucket = self.client.bucket(self.bucket_name)
        return self._bucket

    def _get_blob_path(self, path: str) -> str:
        """
        Convert a relative path to a full blob path with prefix.

        Args:
            path: Relative path (e.g., "runs/run-123/input/envelope.json")

        Returns:
            Full blob path with prefix (e.g., "private/runs/run-123/input/envelope.json")
        """
        # Remove leading slashes from path
        clean_path = path.lstrip("/")

        # Combine prefix and path
        if self.prefix:
            return f"{self.prefix.rstrip('/')}/{clean_path}"
        return clean_path

    def write(self, path: str, content: bytes | str) -> str:
        """Write content to GCS."""
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        if isinstance(content, str):
            blob.upload_from_string(content, content_type="text/plain; charset=utf-8")
        else:
            blob.upload_from_string(content)

        logger.debug(
            "Wrote %d bytes to gs://%s/%s",
            len(content),
            self.bucket_name,
            blob_path,
        )
        return self.get_uri(path)

    def write_file(self, path: str, local_path: Path) -> str:
        """Upload a local file to GCS."""
        if not local_path.exists():
            msg = f"Local file does not exist: {local_path}"
            raise FileNotFoundError(msg)

        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        blob.upload_from_filename(str(local_path))

        logger.debug(
            "Uploaded %s to gs://%s/%s",
            local_path,
            self.bucket_name,
            blob_path,
        )
        return self.get_uri(path)

    def read(self, path: str) -> bytes:
        """Read content from GCS."""
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        if not blob.exists():
            msg = f"File does not exist: gs://{self.bucket_name}/{blob_path}"
            raise FileNotFoundError(msg)

        return blob.download_as_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read text content from GCS."""
        content = self.read(path)
        return content.decode(encoding)

    def download(self, path: str, local_path: Path) -> None:
        """Download a file from GCS to local filesystem."""
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        if not blob.exists():
            msg = f"File does not exist: gs://{self.bucket_name}/{blob_path}"
            raise FileNotFoundError(msg)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))

        logger.debug(
            "Downloaded gs://%s/%s to %s",
            self.bucket_name,
            blob_path,
            local_path,
        )

    def exists(self, path: str) -> bool:
        """Check if path exists in GCS."""
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)
        return blob.exists()

    def delete(self, path: str) -> None:
        """Delete a file from GCS."""
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        if not blob.exists():
            msg = f"File does not exist: gs://{self.bucket_name}/{blob_path}"
            raise FileNotFoundError(msg)

        blob.delete()
        logger.debug("Deleted gs://%s/%s", self.bucket_name, blob_path)

    def delete_prefix(self, prefix: str) -> int:
        """
        Delete all files under a prefix.

        For GCS, this lists all blobs with the prefix and deletes them.

        Args:
            prefix: Path prefix to delete (e.g., "runs/run-123/")

        Returns:
            Number of files deleted
        """
        blob_prefix = self._get_blob_path(prefix.rstrip("/") + "/")

        # List all blobs with this prefix
        blobs = list(self.bucket.list_blobs(prefix=blob_prefix))

        if not blobs:
            return 0

        # Delete all blobs
        for blob in blobs:
            try:
                blob.delete()
                logger.debug("Deleted gs://%s/%s", self.bucket_name, blob.name)
            except Exception:
                logger.exception(
                    "Failed to delete gs://%s/%s",
                    self.bucket_name,
                    blob.name,
                )
                raise

        logger.info(
            "Deleted %d files under gs://%s/%s",
            len(blobs),
            self.bucket_name,
            blob_prefix,
        )
        return len(blobs)

    def get_uri(self, path: str) -> str:
        """Get gs:// URI for a path."""
        blob_path = self._get_blob_path(path)
        return f"gs://{self.bucket_name}/{blob_path}"

    def get_download_url(
        self,
        path: str,
        *,
        expires_in: int = 3600,
        filename: str | None = None,
    ) -> str:
        """
        Get a signed URL for downloading a file.

        Uses GCS signed URLs which provide time-limited access without
        requiring authentication headers. This is how users download
        private files (submissions, validation outputs, artifacts).

        IMPORTANT: Signed URL generation requires the service account to have
        the `iam.serviceAccounts.signBlob` permission, or you need to use
        a downloaded service account key file. On Cloud Run with Workload
        Identity, the default service account can sign URLs automatically.

        Args:
            path: Relative path within storage (without prefix)
            expires_in: URL expiry time in seconds (default: 1 hour)
            filename: Optional filename for Content-Disposition header

        Returns:
            Signed URL for downloading the file

        Raises:
            FileNotFoundError: If the file doesn't exist
        """
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)

        if not blob.exists():
            msg = f"File does not exist: gs://{self.bucket_name}/{blob_path}"
            raise FileNotFoundError(msg)

        # Build response headers for Content-Disposition
        response_disposition = None
        if filename:
            # Sanitize filename for header
            safe_filename = filename.replace('"', '\\"')
            response_disposition = f'attachment; filename="{safe_filename}"'

        # Generate signed URL
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_in),
            method="GET",
            response_disposition=response_disposition,
        )

        return url


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """
    Parse a GCS URI into bucket name and blob path.

    This is a helper function for working with existing GCS URIs.

    Args:
        uri: GCS URI (e.g., 'gs://bucket/path/to/file.json')

    Returns:
        Tuple of (bucket_name, blob_path)

    Raises:
        ValueError: If URI format is invalid

    Example:
        >>> bucket, blob = parse_gcs_uri("gs://my-bucket/private/org/run/input.json")
        >>> bucket
        'my-bucket'
        >>> blob
        'private/org/run/input.json'
    """
    if not uri.startswith("gs://"):
        msg = f"Invalid GCS URI (must start with gs://): {uri}"
        raise ValueError(msg)

    # Remove gs:// prefix
    path = uri[5:]

    # Split into bucket and blob
    parts = path.split("/", 1)
    if len(parts) != 2:  # noqa: PLR2004
        msg = f"Invalid GCS URI (must be gs://bucket/path): {uri}"
        raise ValueError(msg)

    bucket_name, blob_path = parts
    return bucket_name, blob_path
