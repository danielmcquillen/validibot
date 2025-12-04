"""
GCS client utilities for validator containers.

Provides helpers for downloading input envelopes and uploading output envelopes
to Google Cloud Storage.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from typing import TypeVar

from google.cloud import storage
from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """
    Parse a GCS URI into bucket and blob path.

    Args:
        uri: GCS URI like 'gs://bucket-name/path/to/file.json'

    Returns:
        Tuple of (bucket_name, blob_path)

    Raises:
        ValueError: If URI is not a valid GCS URI
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): {uri}")

    uri_without_scheme = uri[5:]  # Remove 'gs://'
    parts = uri_without_scheme.split("/", 1)

    if len(parts) != 2:  # noqa: PLR2004
        raise ValueError(f"Invalid GCS URI (missing path): {uri}")

    bucket_name, blob_path = parts
    return bucket_name, blob_path


def download_envelope[T: BaseModel](uri: str, envelope_class: type[T]) -> T:
    """
    Download and deserialize a Pydantic envelope from GCS.

    Args:
        uri: GCS URI to the envelope JSON file
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If URI is invalid or file doesn't exist
        ValidationError: If JSON doesn't match envelope schema
    """
    logger.info("Downloading envelope from %s", uri)

    bucket_name, blob_path = parse_gcs_uri(uri)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    if not blob.exists():
        raise ValueError(f"Envelope not found at {uri}")

    # Download as string and deserialize
    json_content = blob.download_as_text()
    envelope = envelope_class.model_validate_json(json_content)

    logger.info(
        "Successfully downloaded %s envelope (run_id=%s)",
        envelope_class.__name__,
        getattr(envelope, "run_id", "unknown"),
    )

    return envelope


def upload_envelope(envelope: BaseModel, uri: str) -> None:
    """
    Serialize and upload a Pydantic envelope to GCS.

    Args:
        envelope: Pydantic model instance to upload
        uri: GCS URI where the envelope should be uploaded

    Raises:
        ValueError: If URI is invalid
    """
    logger.info("Uploading %s to %s", envelope.__class__.__name__, uri)

    bucket_name, blob_path = parse_gcs_uri(uri)

    # Serialize to JSON
    json_content = envelope.model_dump_json(indent=2, exclude_none=True)

    # Upload to GCS
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    blob.upload_from_string(
        json_content,
        content_type="application/json",
    )

    logger.info("Successfully uploaded envelope to %s", uri)


def download_file(uri: str, destination: Path) -> None:
    """
    Download a file from GCS to local filesystem.

    Args:
        uri: GCS URI to the file
        destination: Local path where file should be saved

    Raises:
        ValueError: If URI is invalid or file doesn't exist
    """
    logger.info("Downloading file from %s to %s", uri, destination)

    bucket_name, blob_path = parse_gcs_uri(uri)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    if not blob.exists():
        raise ValueError(f"File not found at {uri}")

    # Ensure parent directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Download file
    blob.download_to_filename(str(destination))

    logger.info(
        "Successfully downloaded file to %s (%d bytes)",
        destination,
        destination.stat().st_size,
    )


def upload_file(source: Path, uri: str, content_type: str | None = None) -> None:
    """
    Upload a file from local filesystem to GCS.

    Args:
        source: Local path to the file
        uri: GCS URI where file should be uploaded
        content_type: Optional MIME type for the file

    Raises:
        ValueError: If URI is invalid or source file doesn't exist
    """
    if not source.exists():
        raise ValueError(f"Source file does not exist: {source}")

    logger.info("Uploading file from %s to %s", source, uri)

    bucket_name, blob_path = parse_gcs_uri(uri)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    blob.upload_from_filename(
        str(source),
        content_type=content_type,
    )

    logger.info(
        "Successfully uploaded file to %s (%d bytes)", uri, source.stat().st_size
    )


def upload_directory(
    source_dir: Path, base_uri: str, manifest_path: str = "manifest.json"
) -> dict:
    """
    Upload an entire directory to GCS and create a manifest.

    Args:
        source_dir: Local directory to upload
        base_uri: GCS URI prefix (e.g., 'gs://bucket/org_id/run_id/outputs/')
        manifest_path: Relative path for manifest file within base_uri

    Returns:
        Manifest dict with file listings

    Raises:
        ValueError: If source_dir doesn't exist or base_uri is invalid
    """
    if not source_dir.exists():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    logger.info("Uploading directory %s to %s", source_dir, base_uri)

    # Ensure base_uri ends with /
    if not base_uri.endswith("/"):
        base_uri += "/"

    files_uploaded = []

    # Upload all files in directory
    for file_path in source_dir.rglob("*"):
        if file_path.is_file():
            # Calculate relative path from source_dir
            rel_path = file_path.relative_to(source_dir)
            file_uri = f"{base_uri}{rel_path.as_posix()}"

            # Upload file
            upload_file(file_path, file_uri)

            files_uploaded.append(
                {
                    "name": rel_path.as_posix(),
                    "uri": file_uri,
                    "size_bytes": file_path.stat().st_size,
                }
            )

    # Create manifest
    manifest = {
        "format": "directory",
        "base_uri": base_uri,
        "files": files_uploaded,
        "total_files": len(files_uploaded),
        "total_bytes": sum(f["size_bytes"] for f in files_uploaded),
    }

    # Upload manifest
    manifest_uri = f"{base_uri}{manifest_path}"
    bucket_name, blob_path = parse_gcs_uri(manifest_uri)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    blob.upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )

    logger.info("Uploaded %d files, manifest at %s", len(files_uploaded), manifest_uri)

    manifest["manifest_uri"] = manifest_uri
    return manifest
