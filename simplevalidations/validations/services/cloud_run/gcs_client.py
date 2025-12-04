"""
Google Cloud Storage client for validation envelopes.

This module provides functions to upload and download validation envelopes
to/from GCS. It's a thin, focused wrapper around google-cloud-storage.

Design: Simple functions that do one thing well. No stateful client objects.
"""

from pathlib import Path

from google.cloud import storage
from pydantic import BaseModel


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """
    Parse a GCS URI into bucket name and blob path.

    Args:
        uri: GCS URI (e.g., 'gs://bucket/path/to/file.json')

    Returns:
        Tuple of (bucket_name, blob_path)

    Raises:
        ValueError: If URI format is invalid

    Example:
        >>> bucket, blob = parse_gcs_uri("gs://my-bucket/org/run/input.json")
        >>> bucket
        'my-bucket'
        >>> blob
        'org/run/input.json'
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


def upload_envelope(
    envelope: BaseModel,
    uri: str,
) -> None:
    """
    Upload a Pydantic envelope to GCS as JSON.

    Args:
        envelope: Any Pydantic model (ValidationInputEnvelope, etc.)
        uri: GCS URI (e.g., 'gs://bucket/org_id/run_id/input.json')

    Raises:
        ValueError: If URI is invalid
        google.cloud.exceptions.GoogleCloudError: If upload fails

    Example:
        >>> from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope
        >>> envelope = EnergyPlusInputEnvelope(...)
        >>> upload_envelope(envelope, "gs://my-bucket/runs/abc-123/input.json")
    """
    bucket_name, blob_path = parse_gcs_uri(uri)

    # Initialize GCS client
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    # Serialize envelope to JSON
    json_data = envelope.model_dump_json(indent=2)

    # Upload to GCS
    blob.upload_from_string(
        json_data,
        content_type="application/json",
    )


def download_envelope(
    uri: str,
    envelope_class: type[BaseModel],
) -> BaseModel:
    """
    Download and deserialize a Pydantic envelope from GCS.

    Args:
        uri: GCS URI to the envelope JSON
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If URI is invalid or file doesn't exist
        ValidationError: If JSON doesn't match envelope schema
        google.cloud.exceptions.GoogleCloudError: If download fails

    Example:
        >>> from sv_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
        >>> envelope = download_envelope(
        ...     "gs://my-bucket/runs/abc-123/output.json",
        ...     EnergyPlusOutputEnvelope,
        ... )
    """
    bucket_name, blob_path = parse_gcs_uri(uri)

    # Initialize GCS client
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    # Check if blob exists
    if not blob.exists():
        msg = f"File does not exist: {uri}"
        raise ValueError(msg)

    # Download JSON
    json_data = blob.download_as_text()

    # Deserialize to Pydantic model
    envelope = envelope_class.model_validate_json(json_data)
    return envelope


def upload_file(
    local_path: Path,
    uri: str,
    content_type: str | None = None,
) -> None:
    """
    Upload a local file to GCS.

    Args:
        local_path: Path to local file
        uri: GCS URI destination
        content_type: Optional MIME type

    Raises:
        FileNotFoundError: If local file doesn't exist
        ValueError: If URI is invalid
        google.cloud.exceptions.GoogleCloudError: If upload fails

    Example:
        >>> from pathlib import Path
        >>> upload_file(
        ...     Path("/tmp/model.idf"),
        ...     "gs://my-bucket/models/abc-123.idf",
        ...     content_type="application/octet-stream",
        ... )
    """
    if not local_path.exists():
        msg = f"Local file does not exist: {local_path}"
        raise FileNotFoundError(msg)

    bucket_name, blob_path = parse_gcs_uri(uri)

    # Initialize GCS client
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    # Upload file
    blob.upload_from_filename(
        str(local_path),
        content_type=content_type,
    )


def download_file(
    uri: str,
    local_path: Path,
) -> None:
    """
    Download a file from GCS to local filesystem.

    Args:
        uri: GCS URI to download from
        local_path: Local path to save file to

    Raises:
        ValueError: If URI is invalid or file doesn't exist
        google.cloud.exceptions.GoogleCloudError: If download fails

    Example:
        >>> from pathlib import Path
        >>> download_file(
        ...     "gs://my-bucket/models/abc-123.idf",
        ...     Path("/tmp/model.idf"),
        ... )
    """
    bucket_name, blob_path = parse_gcs_uri(uri)

    # Initialize GCS client
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    # Check if blob exists
    if not blob.exists():
        msg = f"File does not exist: {uri}"
        raise ValueError(msg)

    # Create parent directory if it doesn't exist
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Download file
    blob.download_to_filename(str(local_path))
