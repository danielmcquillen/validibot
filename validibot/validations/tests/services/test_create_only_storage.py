"""Tests for provider-neutral create-only local storage primitives.

Validator attempt paths are derived from durable UUIDs, but a unique-looking
path is not enough to make replay safe: stale state or duplicate delivery can
still target it twice. These tests prove that local files and directories are
published once, conflicts preserve the first committed bytes, and temporary
files do not leak into an attempt bundle.
"""

from __future__ import annotations

import os

import pytest

from validibot.validations.services.create_only_storage import StorageConflictError
from validibot.validations.services.create_only_storage import create_local_bytes
from validibot.validations.services.create_only_storage import create_local_directory
from validibot.validations.services.create_only_storage import create_local_file

TEST_FILE_MODE = 0o640
TEST_DIRECTORY_MODE = 0o750


def test_create_local_bytes_publishes_complete_content_and_mode(tmp_path):
    """A successful publish must expose the requested bytes and permissions."""
    destination = tmp_path / "attempt" / "input.json"

    create_local_bytes(destination, b'{"state":"ready"}', mode=TEST_FILE_MODE)

    assert destination.read_bytes() == b'{"state":"ready"}'
    assert destination.stat().st_mode & 0o777 == TEST_FILE_MODE
    assert list(destination.parent.glob(".*.part")) == []


def test_create_local_bytes_rejects_replay_and_preserves_first_writer(tmp_path):
    """Even identical replay must conflict so one attempt has one producer."""
    destination = tmp_path / "input.json"
    create_local_bytes(destination, b"first")

    with pytest.raises(StorageConflictError, match="already exists"):
        create_local_bytes(destination, b"second")

    assert destination.read_bytes() == b"first"


def test_create_local_bytes_rejects_broken_symlink_destination(tmp_path):
    """A broken symlink is still an occupied identity and must not be followed."""
    destination = tmp_path / "input.json"
    destination.symlink_to(tmp_path / "missing-target")

    with pytest.raises(StorageConflictError, match="already exists"):
        create_local_bytes(destination, b"replacement")

    assert destination.is_symlink()


def test_create_local_file_copies_once_without_mutating_source(tmp_path):
    """Resource staging must copy exact bytes while leaving its source intact."""
    source = tmp_path / "weather.epw"
    destination = tmp_path / "attempt" / "resources" / "weather.epw"
    source.write_bytes(b"weather-data")

    create_local_file(source, destination)

    assert destination.read_bytes() == b"weather-data"
    assert source.read_bytes() == b"weather-data"
    with pytest.raises(StorageConflictError, match="already exists"):
        create_local_file(source, destination)


def test_create_local_directory_reserves_attempt_identity_once(tmp_path):
    """Local async dispatch must fail if the same attempt bundle is prepared twice."""
    destination = tmp_path / "runs" / "run-1" / "attempts" / "attempt-1"

    create_local_directory(destination, mode=TEST_DIRECTORY_MODE)

    assert destination.is_dir()
    assert destination.stat().st_mode & 0o777 == TEST_DIRECTORY_MODE
    with pytest.raises(StorageConflictError, match="already exists"):
        create_local_directory(destination)


def test_create_local_bytes_cleans_temporary_file_after_publish_race(
    tmp_path,
    monkeypatch,
):
    """A competing winner between preflight and publish must leave no partial file."""
    destination = tmp_path / "input.json"
    real_link = os.link

    def competing_link(source, target):
        destination.write_bytes(b"winner")
        return real_link(source, target)

    monkeypatch.setattr(os, "link", competing_link)

    with pytest.raises(StorageConflictError, match="already exists"):
        create_local_bytes(destination, b"loser")

    assert destination.read_bytes() == b"winner"
    assert list(tmp_path.glob(".*.part")) == []
