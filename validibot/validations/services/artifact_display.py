"""Presentation and download helpers for run artifacts.

Artifacts can be backed by Django file storage, local run-workspace files, or
remote object storage. This module keeps those storage details out of templates:
the UI receives safe display rows and the download view receives an opened file
only when the web process can serve the bytes without exposing raw storage URIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO
from typing import TYPE_CHECKING
from urllib.parse import unquote
from urllib.parse import urlparse

from validibot.core.storage.local import LocalDataStorage
from validibot.core.utils import reverse_with_org
from validibot.validations.services.run_workspace import CONTAINER_ATTEMPTS_DIR

if TYPE_CHECKING:
    from django.http import HttpRequest

    from validibot.validations.models import Artifact
    from validibot.validations.models import ValidationRun

DEFAULT_ARTIFACT_CONTENT_TYPE = "application/octet-stream"
ARTIFACT_HASH_DISPLAY_LENGTH = 16


class ArtifactDownloadUnavailableError(Exception):
    """Raised when an artifact has no directly downloadable local source."""


@dataclass(frozen=True)
class ArtifactDisplayItem:
    """Template-safe representation of one run artifact.

    The item deliberately excludes ``storage_uri`` and ``manifest_uri`` because
    those are internal storage addresses, not public UI URLs.
    """

    artifact: Artifact
    detail_url: str
    download_url: str
    filename: str
    producer_step: str
    role: str
    kind: str
    content_type: str
    data_format: str
    retention_class: str
    size_bytes: int
    sha256: str
    sha256_short: str
    download_available: bool


@dataclass(frozen=True)
class ArtifactDownloadSource:
    """Opened file object and response metadata for an artifact download."""

    fileobj: IO[bytes]
    filename: str
    content_type: str
    sha256: str


def build_artifact_display_items(
    *,
    run: ValidationRun,
    request: HttpRequest,
) -> list[ArtifactDisplayItem]:
    """Return all artifacts for ``run`` as safe display items."""

    artifacts = run.artifacts.select_related("workflow_step", "step_run").order_by(
        "workflow_step__order",
        "step_run__step_order",
        "contract_key",
        "item_key",
        "pk",
    )
    return [
        build_artifact_display_item(artifact=artifact, request=request)
        for artifact in artifacts
    ]


def build_artifact_display_item(
    *,
    artifact: Artifact,
    request: HttpRequest,
) -> ArtifactDisplayItem:
    """Build one template-safe artifact display item."""

    detail_url = reverse_with_org(
        "validations:artifact_detail",
        request=request,
        kwargs={
            "pk": artifact.validation_run_id,
            "artifact_pk": artifact.pk,
        },
    )
    download_url = reverse_with_org(
        "validations:artifact_download",
        request=request,
        kwargs={
            "pk": artifact.validation_run_id,
            "artifact_pk": artifact.pk,
        },
    )
    sha256 = artifact.sha256 or ""
    return ArtifactDisplayItem(
        artifact=artifact,
        detail_url=detail_url,
        download_url=download_url,
        filename=get_artifact_filename(artifact),
        producer_step=get_artifact_producer_step(artifact),
        role=artifact.role or "",
        kind=artifact.get_kind_display(),
        content_type=artifact.content_type or "",
        data_format=artifact.data_format or "",
        retention_class=artifact.retention_class or "",
        size_bytes=artifact.size_bytes or 0,
        sha256=sha256,
        sha256_short=_short_hash(sha256),
        download_available=is_artifact_download_available(artifact),
    )


def get_artifact_filename(artifact: Artifact) -> str:
    """Return the best human-facing filename for an artifact."""

    if artifact.storage_uri:
        filename = _filename_from_uri(artifact.storage_uri)
        if filename:
            return filename
    if artifact.file and artifact.file.name:
        filename = Path(artifact.file.name).name
        if filename:
            return filename
    return artifact.label or f"artifact-{artifact.pk}"


def get_artifact_producer_step(artifact: Artifact) -> str:
    """Return a concise label for the step that produced ``artifact``."""

    workflow_step = artifact.workflow_step
    if workflow_step is not None:
        step_name = workflow_step.name or "Step"
        step_key = getattr(workflow_step, "step_key", "") or ""
        if step_key:
            return f"{step_name} ({step_key})"
        return step_name
    if artifact.step_run_id:
        return f"Step {artifact.step_run.step_order}"
    return "Run"


def is_artifact_download_available(artifact: Artifact) -> bool:
    """Return whether the UI should show a direct download action."""

    if artifact.file and artifact.file.name:
        return True
    path = resolve_local_artifact_path(artifact)
    return path is not None and path.is_file()


def open_artifact_download(artifact: Artifact) -> ArtifactDownloadSource:
    """Open a directly downloadable artifact source.

    Raises:
        ArtifactDownloadUnavailableError: The artifact has no local or
            storage-backed file source that can be served safely.
    """

    filename = get_artifact_filename(artifact)
    content_type = artifact.content_type or DEFAULT_ARTIFACT_CONTENT_TYPE

    if artifact.file and artifact.file.name:
        try:
            artifact.file.open("rb")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ArtifactDownloadUnavailableError(str(exc)) from exc
        return ArtifactDownloadSource(
            fileobj=artifact.file,
            filename=filename,
            content_type=content_type,
            sha256=artifact.sha256 or "",
        )

    path = resolve_local_artifact_path(artifact)
    if path is None:
        msg = "Artifact bytes are not available for direct download."
        raise ArtifactDownloadUnavailableError(msg)
    if not path.is_file():
        msg = "Artifact file is missing."
        raise ArtifactDownloadUnavailableError(msg)

    try:
        fileobj = path.open("rb")
    except OSError as exc:
        raise ArtifactDownloadUnavailableError(str(exc)) from exc

    return ArtifactDownloadSource(
        fileobj=fileobj,
        filename=filename,
        content_type=content_type,
        sha256=artifact.sha256 or "",
    )


def resolve_local_artifact_path(artifact: Artifact) -> Path | None:
    """Resolve a safe local filesystem path for ``artifact.storage_uri``.

    The resolver supports two local forms:

    * ``file://<DATA_STORAGE_ROOT>/...`` for files already addressed by the
      worker's local storage root.
    * ``file:///validibot/attempts/<attempt>/output/...`` for
      container-visible output paths, mapped back into the owning attempt's
      workspace after verifying that the attempt belongs to the artifact's
      step/run.

    Other schemes are intentionally unsupported here; remote object storage
    needs a signed-download strategy rather than raw URI exposure.
    """

    parsed = urlparse(artifact.storage_uri or "")
    if parsed.scheme != "file":
        return None

    uri_path = Path(unquote(parsed.path))
    container_path = _resolve_container_output_path(artifact=artifact, path=uri_path)
    if container_path is not None:
        return container_path

    storage = LocalDataStorage()
    if _is_relative_to(uri_path, storage.root):
        return uri_path
    return None


def _resolve_container_output_path(
    *,
    artifact: Artifact,
    path: Path,
) -> Path | None:
    """Map an attempt-bound container output URI to its host workspace."""

    container_attempts_path = Path(CONTAINER_ATTEMPTS_DIR)
    if not _is_relative_to(path, container_attempts_path):
        return None

    relative = path.relative_to(container_attempts_path)
    if len(relative.parts) < 3:  # noqa: PLR2004
        return None
    attempt_id, output_dirname, *artifact_parts = relative.parts
    if (
        output_dirname != "output"
        or not artifact_parts
        or any(part in {"", ".", ".."} for part in artifact_parts)
    ):
        return None

    if artifact.step_run_id:
        attempt_matches = artifact.step_run.execution_attempts.filter(
            pk=attempt_id,
        ).exists()
    else:
        from validibot.validations.models import ExecutionAttempt

        attempt_matches = ExecutionAttempt.objects.filter(
            pk=attempt_id,
            step_run__validation_run_id=artifact.validation_run_id,
        ).exists()
    if not attempt_matches:
        return None

    storage_path = (
        Path("runs")
        / str(artifact.org_id)
        / str(artifact.validation_run_id)
        / "attempts"
        / attempt_id
        / "output"
        / Path(*artifact_parts)
    )
    return _resolve_storage_path(storage_path)


def _filename_from_uri(uri: str) -> str:
    """Return the final path segment from a URI."""

    parsed = urlparse(uri)
    path = parsed.path if parsed.scheme else uri
    return Path(unquote(path)).name


def _short_hash(value: str) -> str:
    """Return a compact SHA-256 prefix for tables."""

    if not value:
        return ""
    if len(value) <= ARTIFACT_HASH_DISPLAY_LENGTH:
        return value
    return f"{value[:ARTIFACT_HASH_DISPLAY_LENGTH]}..."


def _resolve_storage_path(path: Path) -> Path:
    """Resolve ``path`` under ``DATA_STORAGE_ROOT`` with traversal protection."""

    storage_root = LocalDataStorage().root
    full_path = storage_root / path.as_posix().lstrip("/")
    full_path.resolve(strict=False).relative_to(
        storage_root.resolve(strict=False),
    )
    return full_path


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Backport-friendly wrapper around ``Path.relative_to``."""

    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True
