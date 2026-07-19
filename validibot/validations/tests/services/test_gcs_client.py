"""
Tests for the GCS client service — envelope upload/download and URI parsing.

The GCS client is the I/O layer between Django and Google Cloud Storage for
advanced validator execution.  It handles three operations:

- **URI parsing** (``parse_gcs_uri``): Splits ``gs://bucket/path/to/blob``
  into bucket name and blob path.  Used by both upload and download.
- **Envelope upload** (``upload_envelope``): Serializes a Pydantic model to
  JSON and uploads it to GCS as the input envelope for a validator container.
- **Envelope download** (``download_envelope``): Downloads JSON from GCS and
  deserializes it into a typed Pydantic model — used by the callback handler
  to retrieve output envelopes after async validation completes.

These tests mock the ``google.cloud.storage.Client`` to avoid needing real
GCS credentials.  No Django models are involved, so no ``@pytest.mark.django_db``
is needed.
"""

import hashlib
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from google.api_core.exceptions import PreconditionFailed
from pydantic import BaseModel

from validibot.validations.services.cloud_run.gcs_client import copy_gcs_file_generation
from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.cloud_run.gcs_client import get_gcs_file_identity
from validibot.validations.services.cloud_run.gcs_client import parse_gcs_uri
from validibot.validations.services.cloud_run.gcs_client import upload_envelope
from validibot.validations.services.cloud_run.gcs_client import upload_envelope_local
from validibot.validations.services.cloud_run.gcs_client import upload_file
from validibot.validations.services.cloud_run.gcs_client import upload_file_from_path
from validibot.validations.services.create_only_storage import StorageConflictError


def test_parse_gcs_uri():
    """A well-formed ``gs://`` URI should split into bucket and blob path.

    The blob path preserves the full directory structure after the bucket
    name — this is critical because ``bucket.blob(path)`` uses the full
    path to locate the object in GCS.
    """
    bucket, blob = parse_gcs_uri("gs://my-bucket/path/to/file.json")
    assert bucket == "my-bucket"
    assert blob == "path/to/file.json"


def test_parse_gcs_uri_invalid():
    """Non-GCS, bucket-only, and empty-component URIs should raise ``ValueError``.

    The ``s3://`` check catches accidental AWS URIs in GCP deployments.
    The bucket-only check catches truncated URIs. The empty-component checks
    catch ``gs:///path`` (no bucket) and ``gs://bucket/`` (no object) — both
    would otherwise slip past parsing and fail later inside the GCS client
    with an opaque error, or slip past the callback allowlist's parse step.
    """
    with pytest.raises(ValueError, match="must start with gs://"):
        parse_gcs_uri("s3://bucket/file.json")

    with pytest.raises(ValueError, match="must be gs://bucket/path"):
        parse_gcs_uri("gs://bucket-only")

    with pytest.raises(ValueError, match="empty bucket or path"):
        parse_gcs_uri("gs:///runs/org/run/input.json")  # empty bucket

    with pytest.raises(ValueError, match="empty bucket or path"):
        parse_gcs_uri("gs://bucket/")  # empty object path


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_upload_envelope(mock_storage_client):
    """Uploading a Pydantic model should serialize it to JSON in GCS.

    The upload path splits the ``gs://`` URI into bucket and blob path,
    then calls ``blob.upload_from_string()`` with the JSON payload.
    This verifies both the URI parsing integration and the serialization.
    """

    # Create a simple test model
    class TestModel(BaseModel):
        test_field: str
        test_number: int

    envelope = TestModel(test_field="hello", test_number=42)

    # Mock GCS client
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    mock_storage_client.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    # Upload envelope
    upload_envelope(envelope, "gs://test-bucket/test.json")

    # Verify GCS calls
    mock_client.bucket.assert_called_once_with("test-bucket")
    mock_bucket.blob.assert_called_once_with("test.json")
    mock_blob.upload_from_string.assert_called_once()

    # Verify JSON was uploaded
    call_args = mock_blob.upload_from_string.call_args
    uploaded_json = call_args[0][0]
    assert "test_field" in uploaded_json
    assert "hello" in uploaded_json
    assert "42" in uploaded_json
    assert call_args.kwargs == {
        "content_type": "application/json",
        "if_generation_match": 0,
    }


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_upload_envelope_maps_generation_conflict_to_provider_neutral_error(
    mock_storage_client,
):
    """A pre-existing GCS object must become the shared create-only conflict."""

    class TestModel(BaseModel):
        value: str

    mock_blob = MagicMock()
    mock_blob.upload_from_string.side_effect = PreconditionFailed("object exists")
    mock_storage_client.return_value.bucket.return_value.blob.return_value = mock_blob

    with pytest.raises(
        StorageConflictError,
        match=r"gs://test-bucket/input\.json",
    ):
        upload_envelope(
            TestModel(value="new"),
            "gs://test-bucket/input.json",
        )


def test_upload_envelope_local_rejects_replay_and_preserves_first_bytes(tmp_path):
    """Local async envelopes must follow the same create-only rule as GCS."""

    class TestModel(BaseModel):
        value: str

    destination = tmp_path / "attempt" / "input.json"
    upload_envelope_local(TestModel(value="first"), destination)
    first_bytes = destination.read_bytes()

    with pytest.raises(StorageConflictError, match="already exists"):
        upload_envelope_local(TestModel(value="second"), destination)

    assert destination.read_bytes() == first_bytes


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_upload_file_returns_exact_bytes_and_gcs_generation(mock_storage_client):
    """Dispatch must commit the UTF-8 bytes and generation actually uploaded."""
    mock_blob = MagicMock(generation=1700000000000000)
    mock_storage_client.return_value.bucket.return_value.blob.return_value = mock_blob
    content = "café"
    content_bytes = content.encode()

    identity = upload_file(
        content,
        "gs://test-bucket/inputs/model.txt",
        content_type="text/plain",
    )

    mock_blob.upload_from_string.assert_called_once_with(
        content_bytes,
        content_type="text/plain",
        if_generation_match=0,
    )
    assert identity.uri == "gs://test-bucket/inputs/model.txt"
    assert identity.size_bytes == len(content_bytes)
    assert identity.sha256 == hashlib.sha256(content_bytes).hexdigest()
    assert identity.storage_version == "1700000000000000"


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_upload_file_from_path_returns_source_hash_and_generation(
    mock_storage_client,
    tmp_path,
):
    """Path uploads must identify the same source bytes sent to GCS."""
    mock_blob = MagicMock(generation=1700000000000001)
    mock_storage_client.return_value.bucket.return_value.blob.return_value = mock_blob
    source = tmp_path / "model.idf"
    source.write_bytes(b"Version,24.1;")

    identity = upload_file_from_path(
        source,
        "gs://test-bucket/inputs/model.idf",
        content_type="application/octet-stream",
    )

    mock_blob.upload_from_filename.assert_called_once_with(
        str(source),
        content_type="application/octet-stream",
        if_generation_match=0,
    )
    assert identity.size_bytes == source.stat().st_size
    assert identity.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert identity.storage_version == "1700000000000001"


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_get_gcs_file_identity_combines_durable_hash_with_object_metadata(
    mock_storage_client,
):
    """Existing resources bind their recorded digest to current GCS metadata."""
    mock_blob = MagicMock(size=42, generation=1700000000000002)
    mock_storage_client.return_value.bucket.return_value.blob.return_value = mock_blob
    digest = "b" * 64

    identity = get_gcs_file_identity(
        uri="gs://test-bucket/resources/weather.epw",
        sha256=digest,
    )

    mock_blob.reload.assert_called_once_with()
    assert identity.size_bytes == 42  # noqa: PLR2004
    assert identity.sha256 == digest
    assert identity.storage_version == "1700000000000002"


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_copy_generation_pins_source_and_creates_destination_only(
    mock_storage_client,
):
    """Attempt staging must copy one generation without replacing stale output."""
    client = MagicMock()
    source_bucket = MagicMock()
    destination_bucket = MagicMock()
    source_blob = MagicMock()
    copied_blob = MagicMock(size=42, generation=1700000000000003)
    client.bucket.side_effect = [source_bucket, destination_bucket]
    source_bucket.blob.return_value = source_blob
    source_bucket.copy_blob.return_value = copied_blob
    mock_storage_client.return_value = client

    identity = copy_gcs_file_generation(
        source_uri="gs://assets/weather.epw",
        source_generation="1700000000000002",
        destination_uri="gs://validation/runs/attempt/weather.epw",
        expected_size_bytes=42,
        expected_sha256="c" * 64,
    )

    source_bucket.blob.assert_called_once_with(
        "weather.epw",
        generation=1700000000000002,
    )
    source_bucket.copy_blob.assert_called_once_with(
        source_blob,
        destination_bucket,
        new_name="runs/attempt/weather.epw",
        preserve_acl=False,
        source_generation=1700000000000002,
        if_source_generation_match=1700000000000002,
        if_generation_match=0,
    )
    assert identity.storage_version == "1700000000000003"
    assert identity.sha256 == "c" * 64


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_download_envelope(mock_storage_client):
    """Downloading should deserialize GCS JSON into the specified Pydantic model.

    The callback handler uses this to reconstruct typed output envelopes
    (e.g., ``EnergyPlusOutputEnvelope``) from the JSON that the validator
    container uploaded to GCS after completing its simulation.
    """

    class TestModel(BaseModel):
        test_field: str
        test_number: int

    # Mock GCS client
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    mock_storage_client.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    # Mock blob exists and has JSON content
    mock_blob.exists.return_value = True
    mock_blob.download_as_text.return_value = (
        '{"test_field": "world", "test_number": 99}'
    )

    # Download envelope
    envelope = download_envelope("gs://test-bucket/test.json", TestModel)

    # Verify result
    assert isinstance(envelope, TestModel)
    assert envelope.test_field == "world"
    assert envelope.test_number == 99  # noqa: PLR2004

    # Verify GCS calls
    mock_client.bucket.assert_called_once_with("test-bucket")
    mock_bucket.blob.assert_called_once_with("test.json")
    mock_blob.exists.assert_called_once()
    mock_blob.download_as_text.assert_called_once()


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_download_envelope_not_found(mock_storage_client):
    """Downloading a missing blob should raise ``ValueError``.

    This catches the case where a callback arrives before the container
    has finished uploading its output, or when the output blob was
    garbage-collected.  The callback handler retries on this error.
    """

    class TestModel(BaseModel):
        test_field: str

    # Mock GCS client
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()

    mock_storage_client.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    # Mock blob doesn't exist
    mock_blob.exists.return_value = False

    # Should raise ValueError
    with pytest.raises(ValueError, match="File does not exist"):
        download_envelope("gs://test-bucket/missing.json", TestModel)


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_download_envelope_rejects_oversized(mock_storage_client):
    """An object larger than ``max_bytes`` must be refused BEFORE download.

    The worker passes ``settings.VALIDATION_RESULT_MAX_BYTES`` so a compromised
    or buggy validator can't make it buffer an arbitrarily large ``output.json``
    into memory. We verify the size is checked via metadata and that
    ``download_as_text()`` is never called once the cap is exceeded.
    """

    class TestModel(BaseModel):
        test_field: str

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_storage_client.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob

    mock_blob.exists.return_value = True
    mock_blob.size = 50  # bytes, reported after reload()

    with pytest.raises(ValueError, match="exceeds the configured limit"):
        download_envelope(
            "gs://test-bucket/huge.json",
            TestModel,
            max_bytes=10,
        )

    # The oversized object must never be downloaded.
    mock_blob.download_as_text.assert_not_called()


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_delete_prefix_deletes_all_blobs(mock_storage_client):
    """``delete_prefix`` must list and delete every object under the prefix.

    This is the run-bundle purge primitive. It deletes from the raw
    ``gs://<bucket>/runs/...`` location the launcher writes to (NOT the
    DataStorage ``private/`` prefix), so it must enumerate and delete each blob
    and return the count. We also verify a trailing slash is enforced so
    ``runs/<run>`` can't accidentally match ``runs/<run>-2``.
    """
    from validibot.validations.services.cloud_run.gcs_client import delete_prefix

    mock_client = MagicMock()
    mock_bucket = MagicMock()
    blob_a = MagicMock()
    blob_b = MagicMock()
    mock_storage_client.return_value = mock_client
    mock_client.bucket.return_value = mock_bucket
    mock_bucket.list_blobs.return_value = [blob_a, blob_b]

    count = delete_prefix("gs://test-bucket/runs/org/run")

    assert count == 2  # noqa: PLR2004
    mock_client.bucket.assert_called_once_with("test-bucket")
    # Trailing slash enforced on the listing prefix.
    mock_bucket.list_blobs.assert_called_once_with(prefix="runs/org/run/")
    blob_a.delete.assert_called_once()
    blob_b.delete.assert_called_once()
