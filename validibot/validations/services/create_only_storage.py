"""Provider-neutral create-only primitives for validator attempt storage.

Attempt paths are unique during normal execution, but uniqueness alone does
not protect against stale local files, duplicate task delivery, or concurrent
writers. These helpers publish local files atomically without replacement and
give local and cloud adapters one typed conflict for an identity that has
already been committed.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

CREATE_ONLY_CHUNK_SIZE = 1024 * 1024
DEFAULT_PUBLISHED_FILE_MODE = 0o644


class StorageConflictError(RuntimeError):
    """Raised when a create-only storage identity already exists."""


def create_local_bytes(
    destination: Path,
    content: bytes,
    *,
    mode: int = DEFAULT_PUBLISHED_FILE_MODE,
) -> None:
    """Atomically create ``destination`` from bytes without replacement."""
    with io.BytesIO(content) as source:
        _copy_stream_create_only(source, destination, mode=mode)


def create_local_file(
    source: Path,
    destination: Path,
    *,
    mode: int = DEFAULT_PUBLISHED_FILE_MODE,
) -> None:
    """Atomically copy a regular file without replacing the destination."""
    if not source.is_file():
        msg = f"Create-only source does not exist or is not a file: {source}"
        raise FileNotFoundError(msg)
    with source.open("rb") as source_file:
        _copy_stream_create_only(source_file, destination, mode=mode)


def create_local_directory(destination: Path, *, mode: int | None = None) -> None:
    """Create one directory identity while allowing its parents to exist."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise _storage_conflict(destination) from exc
    if mode is not None:
        destination.chmod(mode)


def _copy_stream_create_only(
    source: BinaryIO,
    destination: Path,
    *,
    mode: int,
) -> None:
    """Write a temporary sibling and atomically link it into a free name."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_existing_destination(destination)

    fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".part",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as target:
            shutil.copyfileobj(source, target, length=CREATE_ONLY_CHUNK_SIZE)
            os.fchmod(target.fileno(), mode)
        try:
            os.link(temporary_path, destination)
        except FileExistsError as exc:
            raise _storage_conflict(destination) from exc
        temporary_path.unlink()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _reject_existing_destination(destination: Path) -> None:
    """Reject files, directories, symlinks, and broken symlinks."""
    if os.path.lexists(destination):
        raise _storage_conflict(destination)


def _storage_conflict(destination: Path) -> StorageConflictError:
    """Build the consistent local conflict used by every public helper."""
    return StorageConflictError(
        f"Create-only storage identity already exists: file://{destination}",
    )


__all__ = [
    "CREATE_ONLY_CHUNK_SIZE",
    "DEFAULT_PUBLISHED_FILE_MODE",
    "StorageConflictError",
    "create_local_bytes",
    "create_local_directory",
    "create_local_file",
]
