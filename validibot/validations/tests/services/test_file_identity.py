"""Tests for provider-neutral immutable validator file identities.

The application computes these values before dispatch, so a byte-encoding or
streaming regression here would invalidate the backend's integrity check.
"""

import hashlib

from validibot.validations.services.file_identity import local_bytes_identity
from validibot.validations.services.file_identity import local_file_identity


def test_local_file_identity_hashes_large_files_incrementally(tmp_path):
    """File hashing must cover every byte even when reads cross chunk boundaries."""
    content = (b"validator-input" * 100_000) + b"tail"
    path = tmp_path / "model.bin"
    path.write_bytes(content)
    uri = "file:///validibot/attempts/attempt-1/input/model.bin"

    identity = local_file_identity(path=path, uri=uri)

    digest = hashlib.sha256(content).hexdigest()
    assert identity.uri == uri
    assert identity.size_bytes == len(content)
    assert identity.sha256 == digest
    assert identity.storage_version == f"sha256:{digest}"


def test_local_bytes_identity_hashes_the_exact_encoded_payload():
    """In-memory uploads must describe the bytes passed to the storage adapter."""
    content = "café".encode()

    identity = local_bytes_identity(
        content=content,
        uri="file:///validibot/attempts/attempt-1/input/model.txt",
    )

    digest = hashlib.sha256(content).hexdigest()
    assert identity.size_bytes == len(content)
    assert identity.sha256 == digest
    assert identity.storage_version == f"sha256:{digest}"
