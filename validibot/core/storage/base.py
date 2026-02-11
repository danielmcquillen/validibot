"""
Abstract base class for data storage backends.

Data storage handles validation pipeline files (submissions, envelopes, outputs).
These files are NEVER publicly accessible - they require authentication and
authorization to access.

This is separate from Django's media storage (STORAGES setting) which handles
public files like user avatars and workflow images.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from pathlib import Path  # noqa: TC003 - Used at runtime in type annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


class DataStorage(ABC):
    """
    Abstract base class for validation data storage.

    Implementations handle storage of:
    - Submission files (user-uploaded files to validate)
    - Input envelopes (JSON configuration for validators)
    - Output envelopes (JSON results from validators)
    - Generated files (reports, transformed files, etc.)

    All paths are relative to the storage root. Implementations translate
    these to their backing store (local filesystem, GCS bucket, S3 bucket).

    Example paths:
        runs/{run_id}/input/envelope.json
        runs/{run_id}/output/envelope.json
        runs/{run_id}/input/submission.idf
        runs/{run_id}/output/artifacts/report.html
    """

    @abstractmethod
    def write(self, path: str, content: bytes | str) -> str:
        """
        Write content to storage.

        Args:
            path: Relative path within storage
                (e.g., "runs/run-123/input/envelope.json")
            content: File content as bytes or string

        Returns:
            Full URI of stored file (e.g., "gs://bucket/path" or "file:///app/data/path")
        """

    @abstractmethod
    def write_file(self, path: str, local_path: Path) -> str:
        """
        Upload a local file to storage.

        Args:
            path: Relative path within storage
            local_path: Path to local file to upload

        Returns:
            Full URI of stored file

        Raises:
            FileNotFoundError: If local_path doesn't exist
        """

    @abstractmethod
    def read(self, path: str) -> bytes:
        """
        Read content from storage.

        Args:
            path: Relative path within storage

        Returns:
            File content as bytes

        Raises:
            FileNotFoundError: If path doesn't exist
        """

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """
        Read text content from storage.

        Args:
            path: Relative path within storage
            encoding: Text encoding (default: utf-8)

        Returns:
            File content as string

        Raises:
            FileNotFoundError: If path doesn't exist
        """

    @abstractmethod
    def download(self, path: str, local_path: Path) -> None:
        """
        Download a file from storage to local filesystem.

        Args:
            path: Relative path within storage
            local_path: Local path to save file to

        Raises:
            FileNotFoundError: If storage path doesn't exist
        """

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if a path exists in storage.

        Args:
            path: Relative path within storage

        Returns:
            True if path exists, False otherwise
        """

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Delete a file from storage.

        Args:
            path: Relative path within storage

        Raises:
            FileNotFoundError: If path doesn't exist
        """

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """
        Delete all files under a prefix (directory).

        This is used for cleaning up validation run directories, e.g.:
            storage.delete_prefix("runs/run-123/")

        Args:
            prefix: Path prefix to delete (e.g., "runs/run-123/")

        Returns:
            Number of files deleted

        Note:
            - Safe to call if prefix doesn't exist (returns 0)
            - For local storage, removes the directory tree
            - For cloud storage, deletes all objects with the prefix
        """

    @abstractmethod
    def get_uri(self, path: str) -> str:
        """
        Get the full URI for a path.

        This returns the internal URI used by the storage system:
        - Local: file:///app/storage/runs/run-123/input/envelope.json
        - GCS: gs://bucket/private/runs/run-123/input/envelope.json
        - S3: s3://bucket/private/runs/run-123/input/envelope.json

        Args:
            path: Relative path within storage

        Returns:
            Full URI for the path
        """

    @abstractmethod
    def get_download_url(
        self,
        path: str,
        *,
        expires_in: int = 3600,
        filename: str | None = None,
    ) -> str:
        """
        Get a signed/authenticated URL for downloading a file.

        For cloud storage, this returns a signed URL that expires.
        For local storage, this returns a URL to the download endpoint.

        Args:
            path: Relative path within storage
            expires_in: URL expiry time in seconds (default: 1 hour)
            filename: Optional filename for Content-Disposition header

        Returns:
            URL that can be used to download the file

        Raises:
            FileNotFoundError: If path doesn't exist
        """

    def write_envelope(self, path: str, envelope: BaseModel) -> str:
        """
        Write a Pydantic envelope model to storage as JSON.

        Args:
            path: Relative path within storage (should end in .json)
            envelope: Pydantic model instance

        Returns:
            Full URI of stored file
        """
        json_content = envelope.model_dump_json(indent=2)
        return self.write(path, json_content)

    def read_envelope(self, path: str, envelope_class: type[BaseModel]) -> BaseModel:
        """
        Read and deserialize a Pydantic envelope from storage.

        Args:
            path: Relative path within storage
            envelope_class: Pydantic model class to deserialize into

        Returns:
            Deserialized envelope instance

        Raises:
            FileNotFoundError: If path doesn't exist
            ValidationError: If JSON doesn't match envelope schema
        """
        json_content = self.read_text(path)
        return envelope_class.model_validate_json(json_content)

    @staticmethod
    def parse_uri(uri: str) -> tuple[str, str]:
        """
        Parse a storage URI into scheme and path.

        Args:
            uri: Full URI (e.g., "gs://bucket/path", "file:///path", "s3://bucket/path")

        Returns:
            Tuple of (scheme, path) where scheme is "gs", "s3", "file", etc.

        Raises:
            ValueError: If URI format is invalid
        """
        if "://" not in uri:
            msg = f"Invalid storage URI (missing ://): {uri}"
            raise ValueError(msg)

        scheme, rest = uri.split("://", 1)
        return scheme, rest
