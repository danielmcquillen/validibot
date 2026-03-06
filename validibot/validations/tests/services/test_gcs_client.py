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

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.cloud_run.gcs_client import parse_gcs_uri
from validibot.validations.services.cloud_run.gcs_client import upload_envelope


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
    """Non-GCS URIs and bucket-only URIs should raise ``ValueError``.

    The ``s3://`` check catches accidental AWS URIs in GCP deployments.
    The bucket-only check catches truncated URIs that would cause a
    confusing empty-blob error downstream.
    """
    with pytest.raises(ValueError, match="must start with gs://"):
        parse_gcs_uri("s3://bucket/file.json")

    with pytest.raises(ValueError, match="must be gs://bucket/path"):
        parse_gcs_uri("gs://bucket-only")


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
