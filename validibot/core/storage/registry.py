"""
Storage backend registry and factory.

This module provides the central access point for getting the configured
data storage backend. The backend is selected based on the DATA_STORAGE_BACKEND
setting.

Usage:
    from validibot.core.storage import get_data_storage

    storage = get_data_storage()
    storage.write("path/to/file.json", content)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from validibot.core.storage.base import DataStorage

logger = logging.getLogger(__name__)

# Default backend class paths
BACKEND_ALIASES = {
    "local": "validibot.core.storage.local.LocalDataStorage",
    "gcs": "validibot.core.storage.gcs.GCSDataStorage",
    # Future backends:
    # "s3": "validibot.core.storage.s3.S3DataStorage",
}


@lru_cache(maxsize=1)
def get_data_storage() -> DataStorage:
    """
    Get the configured data storage backend.

    The backend is selected based on the DATA_STORAGE_BACKEND setting:
    - "local" (default): Local filesystem storage
    - "gcs": Google Cloud Storage
    - "s3": Amazon S3 (future)
    - Full class path: Custom backend class

    Configuration:
        DATA_STORAGE_BACKEND: Backend type or class path
        DATA_STORAGE_OPTIONS: Dict of options passed to backend constructor

    Returns:
        Configured DataStorage instance (cached singleton)

    Example settings:
        # Local filesystem (default)
        DATA_STORAGE_BACKEND = "local"
        DATA_STORAGE_OPTIONS = {"root": "/app/data"}

        # Google Cloud Storage
        DATA_STORAGE_BACKEND = "gcs"
        DATA_STORAGE_OPTIONS = {"bucket_name": "my-bucket"}

        # Custom backend
        DATA_STORAGE_BACKEND = "myapp.storage.CustomStorage"
        DATA_STORAGE_OPTIONS = {"custom_option": "value"}
    """
    backend_setting = getattr(settings, "DATA_STORAGE_BACKEND", "local")
    options = getattr(settings, "DATA_STORAGE_OPTIONS", {})

    # Resolve alias to full class path
    backend_class_path = BACKEND_ALIASES.get(backend_setting, backend_setting)

    logger.info(
        "Initializing data storage backend: %s",
        backend_class_path,
    )

    # Import and instantiate backend class
    try:
        backend_class = import_string(backend_class_path)
    except ImportError as e:
        msg = f"Could not import data storage backend '{backend_class_path}': {e}"
        raise ImportError(msg) from e

    # Create instance with options
    try:
        instance = backend_class(**options)
    except Exception as e:
        msg = f"Could not instantiate data storage backend '{backend_class_path}': {e}"
        raise RuntimeError(msg) from e

    return instance


def clear_storage_cache() -> None:
    """
    Clear the cached storage backend instance.

    Useful for testing or when settings change at runtime.
    """
    get_data_storage.cache_clear()


def get_storage_for_uri(uri: str) -> DataStorage:
    """
    Get a storage backend appropriate for a given URI.

    This is useful when working with URIs from different storage systems
    (e.g., migrating from local to GCS or vice versa).

    Args:
        uri: Storage URI (file://, gs://, s3://)

    Returns:
        DataStorage instance for that URI scheme

    Raises:
        ValueError: If URI scheme is not supported
    """
    from validibot.core.storage.base import DataStorage

    scheme, _ = DataStorage.parse_uri(uri)

    if scheme == "file":
        from validibot.core.storage.local import LocalDataStorage

        # Extract root from URI path
        # file:///app/data/runs/org/run/file.json -> /app/data
        # We use the default root since the path includes the relative part
        return LocalDataStorage()

    if scheme == "gs":
        from validibot.core.storage.gcs import GCSDataStorage

        # Extract bucket from URI
        # gs://bucket/path/to/file -> bucket
        from validibot.core.storage.gcs import parse_gcs_uri

        bucket_name, _ = parse_gcs_uri(uri)
        return GCSDataStorage(bucket_name=bucket_name)

    if scheme == "s3":
        msg = "S3 storage backend not yet implemented"
        raise NotImplementedError(msg)

    msg = f"Unsupported storage URI scheme: {scheme}"
    raise ValueError(msg)


def path_from_uri(uri: str) -> str:
    """
    Extract the relative path from a storage URI.

    This is useful when you have a full URI but need the relative path
    for use with a storage backend.

    Args:
        uri: Storage URI (file://, gs://, s3://)

    Returns:
        Relative path within the storage

    Examples:
        >>> path_from_uri("file:///app/data/runs/org/run/input.json")
        'runs/org/run/input.json'  # Assumes DATA_STORAGE_ROOT=/app/data

        >>> path_from_uri("gs://bucket/runs/org/run/input.json")
        'runs/org/run/input.json'
    """
    from validibot.core.storage.base import DataStorage

    scheme, rest = DataStorage.parse_uri(uri)

    if scheme == "file":
        # For file:// URIs, we need to strip the DATA_STORAGE_ROOT prefix
        # file:///app/data/runs/org/run/file.json -> runs/org/run/file.json
        from pathlib import Path

        full_path = Path(rest)
        storage_root = getattr(settings, "DATA_STORAGE_ROOT", None)
        if storage_root:
            root_path = Path(storage_root)
            try:
                return str(full_path.relative_to(root_path))
            except ValueError:
                pass  # Path is not under storage root
        # Fallback: return path after /data/ or just the filename
        parts = full_path.parts
        if "data" in parts:
            data_idx = parts.index("data")
            return str(Path(*parts[data_idx + 1 :]))
        return full_path.name

    if scheme == "gs":
        # gs://bucket/path -> path
        from validibot.core.storage.gcs import parse_gcs_uri

        _, blob_path = parse_gcs_uri(uri)
        return blob_path

    if scheme == "s3":
        # s3://bucket/path -> path
        parts = rest.split("/", 1)
        if len(parts) == 2:  # noqa: PLR2004
            return parts[1]
        msg = f"Invalid S3 URI (must be s3://bucket/path): {uri}"
        raise ValueError(msg)

    msg = f"Unsupported storage URI scheme: {scheme}"
    raise ValueError(msg)
