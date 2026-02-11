"""
Tests for gcs_client service.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.cloud_run.gcs_client import parse_gcs_uri
from validibot.validations.services.cloud_run.gcs_client import upload_envelope


def test_parse_gcs_uri():
    """Test GCS URI parsing."""
    bucket, blob = parse_gcs_uri("gs://my-bucket/path/to/file.json")
    assert bucket == "my-bucket"
    assert blob == "path/to/file.json"


def test_parse_gcs_uri_invalid():
    """Test GCS URI parsing with invalid URIs."""
    with pytest.raises(ValueError, match="must start with gs://"):
        parse_gcs_uri("s3://bucket/file.json")

    with pytest.raises(ValueError, match="must be gs://bucket/path"):
        parse_gcs_uri("gs://bucket-only")


@patch("validibot.validations.services.cloud_run.gcs_client.storage.Client")
def test_upload_envelope(mock_storage_client):
    """Test envelope upload to GCS."""

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
    """Test envelope download from GCS."""

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
    """Test envelope download when file doesn't exist."""

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
