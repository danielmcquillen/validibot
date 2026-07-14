"""Materialize submitted artifact-port files for validator dispatch."""

from __future__ import annotations

from pathlib import Path

SUBMITTED_FILE_RESOURCE_ID_PREFIX = "submitted_file_port:"


def submitted_file_resource_id(port_key: str) -> str:
    """Return the local-workspace resource id for a submitted port file."""

    return f"{SUBMITTED_FILE_RESOURCE_ID_PREFIX}{port_key}"


def port_key_from_submitted_file_resource_id(resource_id: str | None) -> str | None:
    """Return the port key encoded in a local materialized-file resource id."""

    if not resource_id or not resource_id.startswith(SUBMITTED_FILE_RESOURCE_ID_PREFIX):
        return None
    return resource_id[len(SUBMITTED_FILE_RESOURCE_ID_PREFIX) :]


def submitted_input_files_for_step(submission, step):
    """Query stored submitted files that satisfy artifact ports on ``step``."""

    if submission is None:
        return []
    return list(
        submission.input_files.filter(
            workflow_step=step,
            file_purged_at__isnull=True,
        )
        .exclude(input_file="")
        .exclude(input_file__isnull=True)
        .order_by("port_key", "pk")
    )


def upload_submitted_input_files_to_gcs(
    *,
    submission,
    step,
    execution_bundle_uri: str,
) -> dict[str, str]:
    """Upload submitted artifact-port files into a Cloud Run execution bundle."""

    from validibot.validations.services.cloud_run.gcs_client import upload_file

    input_file_uris: dict[str, str] = {}
    bundle = execution_bundle_uri.rstrip("/")
    for port_file in submitted_input_files_for_step(submission, step):
        filename = port_file.materialized_filename
        target_uri = f"{bundle}/submitted/{port_file.port_key}/{filename}"
        upload_file(
            content=port_file.read_bytes(),
            uri=target_uri,
            content_type=port_file.content_type or _content_type_for_filename(filename),
        )
        input_file_uris[port_file.port_key] = target_uri
    return input_file_uris


def submitted_file_source_path(port_file) -> Path:
    """Return a local filesystem path for Docker workspace materialization."""

    try:
        path = Path(port_file.input_file.path)
    except (AttributeError, NotImplementedError) as exc:
        msg = (
            "Local Docker dispatch requires submitted port files to be backed "
            "by local filesystem storage."
        )
        raise RuntimeError(msg) from exc
    return path


def _content_type_for_filename(filename: str) -> str:
    """Best-effort content type for submitted artifact files."""

    lowered = filename.lower()
    if lowered.endswith(".epw"):
        return "application/vnd.energyplus.epw"
    if lowered.endswith((".epjson", ".json")):
        return "application/json"
    if lowered.endswith(".idf"):
        return "text/plain"
    return "application/octet-stream"
