"""
Google Cloud Storage client for validation envelopes.

This module provides functions to upload and download validation envelopes
to/from GCS. It's a thin, focused wrapper around google-cloud-storage.

Attempt uploads are create-only: GCS writes use a generation-zero precondition
and local development writes use the provider-neutral atomic helper. Reusing
an already-published identity raises the same ``StorageConflictError`` in both
environments.

Design: Simple functions that do one thing well. No stateful client objects.
"""

from pathlib import Path

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from pydantic import BaseModel

from validibot.validations.services.create_only_storage import StorageConflictError
from validibot.validations.services.create_only_storage import create_local_bytes
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.services.file_identity import local_bytes_identity
from validibot.validations.services.file_identity import local_file_identity


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
    # Reject empty components: ``gs:///path`` (no bucket) and ``gs://bucket/``
    # (no object) both pass the split above but fail later inside the GCS client
    # with an opaque error. Fail here with a clear message instead — and so a
    # malformed URI can't slip past the callback allowlist's parse step.
    if not bucket_name or not blob_path:
        msg = f"Invalid GCS URI (empty bucket or path): {uri}"
        raise ValueError(msg)
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
        StorageConflictError: If the destination object already exists.
        google.cloud.exceptions.GoogleCloudError: If another upload error occurs.

    Example:
        >>> from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
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
    try:
        blob.upload_from_string(
            json_data,
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise _gcs_create_conflict(uri) from exc


def upload_envelope_local(envelope: BaseModel, path: Path) -> None:
    """
    Upload a Pydantic envelope to a local path as JSON.

    Args:
        envelope: Pydantic model instance
        path: Filesystem path to write

    Raises:
        StorageConflictError: If the destination path already exists.
    """
    create_local_bytes(
        path,
        envelope.model_dump_json(indent=2).encode("utf-8"),
    )


def download_envelope(
    uri: str,
    envelope_class: type[BaseModel],
    *,
    max_bytes: int | None = None,
) -> BaseModel:
    """
    Download and deserialize a Pydantic envelope from GCS.

    Args:
        uri: GCS URI to the envelope JSON
        envelope_class: Pydantic model class to deserialize to
        max_bytes: Optional hard cap on the object size. When set, the blob's
            size is read from metadata and checked BEFORE download; an object
            larger than the limit raises ValueError without being fetched. The
            worker uses this to protect itself from a compromised or buggy
            validator writing an oversized ``output.json`` that
            ``download_as_text()`` would otherwise buffer fully into memory.

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If URI is invalid, file doesn't exist, or the object
            exceeds ``max_bytes``
        ValidationError: If JSON doesn't match envelope schema
        google.cloud.exceptions.GoogleCloudError: If download fails

    Example:
        >>> from validibot_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
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

    # Enforce the size cap BEFORE downloading. ``blob.exists()`` doesn't
    # populate metadata, so ``reload()`` to learn ``blob.size``, then refuse
    # anything over the limit — we never want ``download_as_text()`` to pull an
    # unbounded object into worker memory.
    if max_bytes is not None:
        blob.reload()
        if blob.size is not None and blob.size > max_bytes:
            msg = (
                f"Refusing to download {uri}: object size {blob.size} bytes "
                f"exceeds the configured limit of {max_bytes} bytes."
            )
            raise ValueError(msg)

    # Download JSON
    json_data = blob.download_as_text()

    # Deserialize to Pydantic model
    envelope = envelope_class.model_validate_json(json_data)
    return envelope


def delete_prefix(uri_prefix: str) -> int:
    """
    Delete every object under a ``gs://`` prefix and return the count deleted.

    Validation attempt bundles are written here directly by the launcher below
    ``gs://<GCS_VALIDATION_BUCKET>/runs/<org_id>/<run_id>/attempts/<attempt_id>/``
    — NOT through the ``DataStorage`` abstraction (which prepends the
    ``private/`` prefix). Purging a run therefore deletes the parent run prefix
    from this same raw location, or the objects leak in GCS while the run is
    marked purged.

    Args:
        uri_prefix: A ``gs://bucket/prefix/`` URI. A trailing slash is appended
            if missing so ``runs/<run>`` can't also match ``runs/<run>-2``.

    Returns:
        Number of objects deleted (0 if the prefix held nothing).

    Raises:
        ValueError: If ``uri_prefix`` is not a valid ``gs://`` URI.
    """
    bucket_name, blob_prefix = parse_gcs_uri(uri_prefix)
    if not blob_prefix.endswith("/"):
        blob_prefix += "/"

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=blob_prefix))
    for blob in blobs:
        blob.delete()
    return len(blobs)


def upload_file_from_path(
    local_path: Path,
    uri: str,
    content_type: str | None = None,
) -> FileIdentity:
    """
    Upload a local file to GCS.

    Args:
        local_path: Path to local file
        uri: GCS URI destination
        content_type: Optional MIME type

    Raises:
        FileNotFoundError: If local file doesn't exist
        ValueError: If URI is invalid
        StorageConflictError: If the destination object already exists.
        google.cloud.exceptions.GoogleCloudError: If another upload error occurs.

    Example:
        >>> from pathlib import Path
        >>> upload_file_from_path(
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

    source_identity = local_file_identity(path=local_path, uri=uri)

    # Upload file
    try:
        blob.upload_from_filename(
            str(local_path),
            content_type=content_type,
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise _gcs_create_conflict(uri) from exc
    generation = _uploaded_generation(blob=blob, uri=uri)
    return FileIdentity(
        uri=uri,
        size_bytes=source_identity.size_bytes,
        sha256=source_identity.sha256,
        storage_version=generation,
    )


def upload_file(
    content: bytes | str,
    uri: str,
    content_type: str | None = None,
) -> FileIdentity:
    """
    Upload file content (bytes or string) to GCS.

    Args:
        content: File content as bytes or string
        uri: GCS URI destination
        content_type: Optional MIME type

    Raises:
        ValueError: If URI is invalid
        StorageConflictError: If the destination object already exists.
        google.cloud.exceptions.GoogleCloudError: If another upload error occurs.

    Example:
        >>> content = b"Building data..."
        >>> upload_file(
        ...     content,
        ...     "gs://my-bucket/models/abc-123.epjson",
        ...     content_type="application/json",
        ... )
    """
    bucket_name, blob_path = parse_gcs_uri(uri)

    # Initialize GCS client
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    source_identity = local_bytes_identity(content=content_bytes, uri=uri)

    # Upload the exact bytes whose identity is returned to the envelope writer.
    try:
        blob.upload_from_string(
            content_bytes,
            content_type=content_type,
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise _gcs_create_conflict(uri) from exc
    generation = _uploaded_generation(blob=blob, uri=uri)
    return FileIdentity(
        uri=uri,
        size_bytes=source_identity.size_bytes,
        sha256=source_identity.sha256,
        storage_version=generation,
    )


def get_gcs_file_identity(*, uri: str, sha256: str) -> FileIdentity:
    """Return provider metadata bound to an already-recorded content digest.

    Managed resources already persist SHA-256 when they are uploaded. Reading
    current GCS metadata supplies the exact generation and byte size without a
    second full download. If the object changed behind Django's back, the
    backend receives the durable expected digest and rejects the substituted
    generation before execution.
    """
    if not sha256:
        msg = f"Cannot bind GCS object without a recorded SHA-256: {uri}"
        raise ValueError(msg)

    bucket_name, blob_path = parse_gcs_uri(uri)
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_path)
    blob.reload()
    if blob.size is None or blob.generation is None:
        msg = f"GCS object metadata is incomplete for immutable input: {uri}"
        raise ValueError(msg)
    return FileIdentity(
        uri=uri,
        size_bytes=int(blob.size),
        sha256=sha256,
        storage_version=str(blob.generation),
    )


def copy_gcs_file_generation(
    *,
    source_uri: str,
    source_generation: str,
    destination_uri: str,
    expected_size_bytes: int,
    expected_sha256: str,
) -> FileIdentity:
    """Copy one exact GCS generation to a create-only attempt identity.

    Server-side copy avoids routing reusable resources through Django memory.
    The generation precondition pins the source bytes and generation zero pins
    the destination to first publication. The validator later verifies the
    copied bytes against ``expected_sha256`` while streaming them.
    """
    try:
        generation = int(source_generation)
    except (TypeError, ValueError) as exc:
        msg = f"GCS storage version must be a numeric generation: {source_generation!r}"
        raise ValueError(msg) from exc
    if generation <= 0:
        msg = f"GCS generation must be positive: {generation}"
        raise ValueError(msg)

    source_bucket_name, source_blob_path = parse_gcs_uri(source_uri)
    destination_bucket_name, destination_blob_path = parse_gcs_uri(destination_uri)
    client = storage.Client()
    source_bucket = client.bucket(source_bucket_name)
    destination_bucket = client.bucket(destination_bucket_name)
    source_blob = source_bucket.blob(source_blob_path, generation=generation)
    try:
        copied = source_bucket.copy_blob(
            source_blob,
            destination_bucket,
            new_name=destination_blob_path,
            preserve_acl=False,
            source_generation=generation,
            if_source_generation_match=generation,
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise _gcs_create_conflict(destination_uri) from exc

    if copied.generation is None or copied.size is None:
        copied.reload()
    if copied.generation is None or copied.size is None:
        msg = f"GCS copy returned incomplete identity metadata: {destination_uri}"
        raise ValueError(msg)
    if int(copied.size) != expected_size_bytes:
        msg = (
            f"Copied GCS object size mismatch for {destination_uri}: expected "
            f"{expected_size_bytes}, got {copied.size}"
        )
        raise ValueError(msg)
    return FileIdentity(
        uri=destination_uri,
        size_bytes=expected_size_bytes,
        sha256=expected_sha256,
        storage_version=str(copied.generation),
    )


def _uploaded_generation(*, blob, uri: str) -> str:
    """Return the immutable generation assigned to a completed GCS upload."""
    if blob.generation is None:
        blob.reload()
    if blob.generation is None:
        msg = f"GCS did not return an object generation after uploading {uri}"
        raise ValueError(msg)
    return str(blob.generation)


def _gcs_create_conflict(uri: str) -> StorageConflictError:
    """Return the provider-neutral error for a failed generation-zero write."""
    return StorageConflictError(f"Create-only storage identity already exists: {uri}")


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
