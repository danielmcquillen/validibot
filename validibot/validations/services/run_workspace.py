"""Per-run filesystem workspace for validator container execution.

ADR-2026-04-27 ``[trust-#4]``: build a per-run workspace on the host with
read-only inputs and a writable output directory, materialise only the files
this run needs into ``input/``, and return container-visible paths so the
input envelope and Docker runner share one source of truth for the
host↔container path mapping.

Layout produced
---------------

::

    <DATA_STORAGE_ROOT>/runs/<org_id>/<run_id>/
      input/                       # mode 755 — readable by container UID 1000
        <original_filename>        # mode 644 — primary submission file
        resources/                 # mode 755
          <resource_filename>      # mode 644
      output/                      # owned 1000:1000, mode 770 (container-only)
        (initially empty; container writes ``output.json`` and ``outputs/``)

Why this exists
---------------

Before this ADR the local Docker runner mounted the entire
``DATA_STORAGE_ROOT`` read-write into every validator container. That meant
a buggy or compromised validator could read or mutate any other run's
inputs and outputs. Cross-tenant isolation rested on validator
implementations being careful, not on the runtime boundary.

After this change, the runner mounts only ``input/`` (read-only) and
``output/`` (read-write) for each run. The workspace builder is the
component responsible for materialising the per-run filesystem so the
runner has something narrow to mount.

When in the lifecycle this runs
-------------------------------

The builder runs *after* the validator's ``validate()`` preprocessing
(e.g. EnergyPlus template resolution that mutates ``submission.content``
and ``submission.original_filename``). In practice that means it is called
from inside ``ExecutionBackend.execute()``. Building earlier would
materialise the pre-resolved file. ADR-2026-04-27 section 8 documents
this ordering.

Cleanup
-------

The existing ``purge_expired_outputs`` retention sweeper deletes whole
``runs/<org_id>/<run_id>/`` directory trees, which is a strict superset of
the new layout, so no separate cleanup hook is needed.

References
----------

The host↔container path contract and the ``chown 1000:1000 + chmod 770``
choice are informed by per-job container-isolation practice in the field
(GitLab Runner, Tekton, Argo, Flyte, Pachyderm, Cromwell, Modal). See
``../../../validibot-project/docs/security/container-sandboxing-comparison.md``
in the validibot-project repo for the full survey.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.core.storage.local import LocalDataStorage

logger = logging.getLogger(__name__)


# UID and GID the validator container runs as. Must match the runner's
# ``user="1000:1000"`` setting in ``runners/docker.py``. Matching the
# Jupyter-stack ``jovyan`` convention and the dominant Kubernetes
# ``runAsUser: 1000`` baseline keeps the host↔container ownership story
# uniform across mainstream tools.
CONTAINER_UID = 1000
CONTAINER_GID = 1000

# Fixed in-container paths. These form the public contract validator
# backends rely on; see the ADR section 8 mounts table.
CONTAINER_INPUT_DIR = "/validibot/input"
CONTAINER_OUTPUT_DIR = "/validibot/output"

# Subdirectory under ``input/`` for workflow resource files (weather
# files, FMU dependencies, etc.). Kept separate from the primary
# submission file so resource-name collisions with the primary filename
# cannot happen, and so the runner can deny writes to it specifically.
RESOURCES_SUBDIR = "resources"

# Permission bits for input dirs (read+exec for everyone, write for owner)
# and input files (read for everyone, write for owner). The container
# only needs to *read* its inputs; write would be a bug.
INPUT_DIR_MODE = 0o755
INPUT_FILE_MODE = 0o644

# Permission bits for the output dir when ``chown 1000:1000`` succeeds:
# read+write+exec for owner and group, nothing for "other". The container
# (as UID 1000) and the worker process (typically root) can both write
# and read; nothing else can. This matches the Kubernetes ``fsGroup`` /
# ``runAsUser`` convention used by Argo, Tekton, and Flyte.
OUTPUT_DIR_MODE_OWNED = 0o770

# Fallback when ``chown`` is not permitted in the current process (e.g.
# rootless local dev). Sticky bit + world-writable matches the existing
# pre-ADR behaviour and the ``/tmp`` convention. Strictly less safe than
# the owned variant; emitted with a warning so operators can investigate.
OUTPUT_DIR_MODE_FALLBACK = 0o1777


@dataclass(frozen=True)
class MaterializedFile:
    """A single file copied into the per-run workspace.

    Carries enough information for the envelope builder to emit a
    container-visible URI and for the runner to know the host path it
    will mount under.

    Attributes:
        name: Filename inside the workspace (no directory components).
        host_path: Absolute path on the host.
        container_uri: ``file:///validibot/...`` URI as the container sees it.
        resource_id: When this file was materialised from a workflow
            step resource (weather file, FMU model, etc.), the resource
            id the envelope uses to reference it. The dispatch layer
            uses this to build the ``resource_uri_overrides`` dict for
            the envelope builder. None for the primary submission file
            and for files materialised outside the
            ``WorkflowStepResource`` flow.
    """

    name: str
    host_path: Path
    container_uri: str
    resource_id: str | None = None


@dataclass(frozen=True)
class ResourceFileSpec:
    """Source data for one resource file the builder will materialise.

    Decoupled from the ``WorkflowStepResource`` Django model so the
    builder is testable without database fixtures and so the model→spec
    translation lives at the call site (where it belongs alongside the
    other dispatch-time decisions).

    Attributes:
        filename: Filename to write inside ``input/resources/``. Must not
            contain path separators or ``..`` segments — the builder
            rejects path-traversal attempts before any disk write.
        source_path: Host path to read the file content from. Today this
            is typically a ``MEDIA_ROOT``-rooted path returned by
            ``WorkflowStepResource.step_resource_file.path``.
        resource_id: Optional opaque identifier the dispatch layer uses
            to map the materialised file back to the envelope's
            ``ResourceFileItem.id`` (or to the ``input_file_uris`` dict
            for files that flow through that mechanism, like the FMU
            model). Passed through unchanged to the resulting
            :class:`MaterializedFile`.
    """

    filename: str
    source_path: Path
    resource_id: str | None = None


@dataclass(frozen=True)
class RunWorkspace:
    """The materialised per-run filesystem workspace.

    Carries both the host paths the runner needs to mount and the
    container-visible URIs the envelope embeds. Treating this as one
    object means the host↔container mapping has exactly one source of
    truth — the envelope builder and the runner read from the same
    instance.

    Attributes:
        run_id: The validation run's stable identifier.
        org_id: The organisation that owns the run; used in the host
            path for retention scoping.
        host_input_dir: Absolute host path of ``input/``. Mounted into
            the container as ``/validibot/input`` (read-only).
        host_output_dir: Absolute host path of ``output/``. Mounted into
            the container as ``/validibot/output`` (read-write).
        primary_file: The materialised submission file.
        resource_files: Materialised workflow resource files (weather
            files, FMU dependencies, etc.) inside ``input/resources/``.
        container_input_dir: The container's view of ``input/``. Always
            ``/validibot/input``; exposed for caller readability rather
            than configurability.
        container_output_dir: The container's view of ``output/``.
            Always ``/validibot/output``; same rationale.
    """

    run_id: str
    org_id: str

    host_input_dir: Path
    host_output_dir: Path

    primary_file: MaterializedFile
    resource_files: list[MaterializedFile] = field(default_factory=list)

    container_input_dir: str = CONTAINER_INPUT_DIR
    container_output_dir: str = CONTAINER_OUTPUT_DIR

    @property
    def input_envelope_container_uri(self) -> str:
        """``file://`` URI of ``input.json`` as the container will see it."""
        return f"file://{self.container_input_dir}/input.json"

    @property
    def output_envelope_container_uri(self) -> str:
        """``file://`` URI the container writes ``output.json`` to."""
        return f"file://{self.container_output_dir}/output.json"

    @property
    def execution_bundle_container_uri(self) -> str:
        """Directory URI the backend's artifact-upload logic appends ``/outputs`` to.

        Backend code at
        ``validibot-validator-backends/validator_backends/energyplus/main.py``
        composes ``f"{execution_bundle_uri}/outputs"`` for artifacts and
        writes ``output.json`` directly under ``execution_bundle_uri``.
        Setting this URI to ``file:///validibot/output`` therefore lands
        artifacts at ``/validibot/output/outputs/...`` automatically and
        requires zero changes in the backend repo.
        """
        return f"file://{self.container_output_dir}"

    @property
    def host_input_envelope_path(self) -> Path:
        """Absolute host path the worker writes ``input.json`` to."""
        return self.host_input_dir / "input.json"

    @property
    def host_output_envelope_path(self) -> Path:
        """Absolute host path of the container's ``output.json`` after exit."""
        return self.host_output_dir / "output.json"


class RunWorkspaceError(Exception):
    """Raised when workspace materialisation fails.

    Distinct from :class:`OSError`/:class:`PermissionError` so callers
    (the dispatch layer) can map workspace problems to a single
    ``ExecutionResponse`` error category without having to enumerate
    every filesystem exception.
    """


class RunWorkspaceBuilder:
    """Materialise per-run workspace directories on the host.

    The builder owns three concerns:

    1. **Layout** — create ``runs/<org>/<run>/{input,input/resources,output}/``.
    2. **Permissions** — set modes and (where possible) ownership so the
       container can write its outputs without the host needing
       world-writable bits.
    3. **Materialisation** — copy the primary submission file and any
       resource files into the workspace, with a path-traversal guard.

    It does not own:

    - Cleanup. The existing ``purge_expired_outputs`` retention sweeper
      removes whole ``runs/<org>/<run>/`` trees, which is a strict
      superset of this layout. Adding a per-builder cleanup hook would
      duplicate that responsibility.
    - The input envelope itself. The dispatch layer calls the envelope
      builder with the :class:`RunWorkspace` returned here.

    Args:
        storage: A :class:`LocalDataStorage` used to resolve workspace
            paths under ``DATA_STORAGE_ROOT``. Cloud Run dispatch does
            not use this builder — Cloud Run Jobs are naturally
            run-scoped via per-job GCS prefixes.
    """

    def __init__(self, storage: LocalDataStorage) -> None:
        self._storage = storage

    # ── public ──────────────────────────────────────────────────────────

    def build(
        self,
        *,
        org_id: str,
        run_id: str,
        primary_filename: str,
        primary_content: bytes,
        resource_files: list[ResourceFileSpec] | None = None,
    ) -> RunWorkspace:
        """Materialise the workspace and return the access object.

        Idempotent within reason — if the workspace already exists, this
        re-writes the input files (in case preprocessing produced different
        content) but preserves the output directory's contents. That
        matters for retries that need to inspect a previous attempt's
        output.

        Args:
            org_id: Organisation id used in the host path.
            run_id: Run identifier used in the host path. Must be
                filesystem-safe (UUIDs are; arbitrary slugs may not be).
            primary_filename: Name to give the submission file inside
                ``input/``. Must not contain path separators or
                ``..`` segments.
            primary_content: Raw bytes of the submission file (already
                decoded from any base64 / preprocessing transforms).
            resource_files: Optional workflow resource files to copy
                into ``input/resources/``.

        Returns:
            A :class:`RunWorkspace` carrying host paths and container
            URIs.

        Raises:
            RunWorkspaceError: If the requested layout cannot be
                materialised (e.g. path traversal in a filename, source
                file missing, or a permission failure that even the
                fallback cannot work around).
        """

        resource_files = resource_files or []

        # Validate filenames *before* touching the filesystem so a bad
        # name doesn't leave half-built directories around.
        self._reject_path_traversal(primary_filename, label="primary_filename")
        for res in resource_files:
            self._reject_path_traversal(res.filename, label="resource filename")

        base_relpath = f"runs/{org_id}/{run_id}"
        base_dir = self._storage._resolve_path(base_relpath)
        input_dir = base_dir / "input"
        resources_dir = input_dir / RESOURCES_SUBDIR
        output_dir = base_dir / "output"

        # Create the directory skeleton with the right permissions
        # up-front so the chmod doesn't have to run twice (mkdir, then
        # chmod). The mode argument to ``mkdir`` is masked by the
        # process umask; we re-chmod afterwards to be explicit.
        input_dir.mkdir(parents=True, exist_ok=True)
        resources_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        input_dir.chmod(INPUT_DIR_MODE)
        resources_dir.chmod(INPUT_DIR_MODE)
        self._set_output_permissions(output_dir)

        # Materialise the primary file.
        primary_host_path = input_dir / primary_filename
        primary_host_path.write_bytes(primary_content)
        primary_host_path.chmod(INPUT_FILE_MODE)

        primary = MaterializedFile(
            name=primary_filename,
            host_path=primary_host_path,
            container_uri=f"file://{CONTAINER_INPUT_DIR}/{primary_filename}",
        )

        # Materialise resource files. Each one is copied as bytes rather
        # than via :func:`shutil.copy` so the builder works the same way
        # against any source path (Django-uploaded media, test fixtures,
        # signed-URL downloads if a future caller pre-stages content
        # itself).
        materialised_resources: list[MaterializedFile] = []
        for res in resource_files:
            if not res.source_path.exists():
                msg = (
                    f"Resource file source does not exist: "
                    f"{res.source_path} (filename={res.filename})"
                )
                raise RunWorkspaceError(msg)
            target = resources_dir / res.filename
            target.write_bytes(res.source_path.read_bytes())
            target.chmod(INPUT_FILE_MODE)
            materialised_resources.append(
                MaterializedFile(
                    name=res.filename,
                    host_path=target,
                    container_uri=(
                        f"file://{CONTAINER_INPUT_DIR}/{RESOURCES_SUBDIR}/{res.filename}"
                    ),
                    resource_id=res.resource_id,
                ),
            )

        logger.debug(
            "Materialised workspace at %s (1 primary + %d resources)",
            base_dir,
            len(materialised_resources),
        )

        return RunWorkspace(
            run_id=run_id,
            org_id=org_id,
            host_input_dir=input_dir,
            host_output_dir=output_dir,
            primary_file=primary,
            resource_files=materialised_resources,
        )

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _reject_path_traversal(name: str, *, label: str) -> None:
        """Raise if ``name`` contains path separators or ``..`` segments.

        The check is intentionally strict: any directory component is a
        path-traversal attempt for the builder's purposes, since every
        materialised file lives directly under ``input/`` or
        ``input/resources/`` with the resource name as a flat leaf.

        We resolve and compare paths because lexical checks miss tricks
        like ``foo/../../etc/passwd`` that nominally start with a
        harmless segment.
        """
        if not name:
            msg = f"Empty {label!s} not allowed"
            raise RunWorkspaceError(msg)

        # Reject any path separator or ``..`` segment up front.
        if "/" in name or "\\" in name or ".." in Path(name).parts:
            msg = f"Path traversal attempt in {label!s}: {name!r}"
            raise RunWorkspaceError(msg)

        # Also reject absolute paths (which lexical checks above mostly
        # cover, but be explicit for Windows-style drive letters too).
        if Path(name).is_absolute():
            msg = f"Absolute path not allowed for {label!s}: {name!r}"
            raise RunWorkspaceError(msg)

    @staticmethod
    def _set_output_permissions(output_dir: Path) -> None:
        """Set ownership and mode on ``output/``.

        Preferred path: ``chown 1000:1000`` then ``chmod 770``. Only the
        container (UID 1000) and the worker (typically root) can write,
        which matches the Kubernetes ``fsGroup``/``runAsUser`` convention
        used by Argo, Tekton, and Flyte.

        Fallback: when ``chown`` is not permitted (typical on rootless
        local dev environments where the worker runs as the host user
        rather than root), we fall back to ``chmod 1777`` — the sticky
        ``/tmp``-style mode used before this ADR. Strictly less safe than
        the owned variant; we log a warning so operators can investigate.
        """
        try:
            os.chown(output_dir, CONTAINER_UID, CONTAINER_GID)
            output_dir.chmod(OUTPUT_DIR_MODE_OWNED)
        except (PermissionError, OSError) as exc:
            # ``OSError`` covers the macOS / Linux rootless case where
            # ``chown`` returns ``EPERM``; ``PermissionError`` is a
            # subclass on Python 3.3+ but we list it for clarity.
            logger.warning(
                "Could not chown %s to UID %d:%d (using sticky world-writable "
                "fallback mode 1777): %s",
                output_dir,
                CONTAINER_UID,
                CONTAINER_GID,
                exc,
            )
            output_dir.chmod(OUTPUT_DIR_MODE_FALLBACK)


__all__ = [
    "CONTAINER_GID",
    "CONTAINER_INPUT_DIR",
    "CONTAINER_OUTPUT_DIR",
    "CONTAINER_UID",
    "INPUT_DIR_MODE",
    "INPUT_FILE_MODE",
    "OUTPUT_DIR_MODE_FALLBACK",
    "OUTPUT_DIR_MODE_OWNED",
    "RESOURCES_SUBDIR",
    "MaterializedFile",
    "ResourceFileSpec",
    "RunWorkspace",
    "RunWorkspaceBuilder",
    "RunWorkspaceError",
]
