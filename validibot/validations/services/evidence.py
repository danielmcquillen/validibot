"""``EvidenceManifestBuilder`` — build + persist evidence manifests for runs.

ADR-2026-04-27 Phase 4 Sessions A + B: the single decision point for
"what goes in a manifest, in what shape." Mirrors the service-class
pattern from
:class:`validibot.workflows.services.versioning.WorkflowVersioningService`:
stateless, static methods, central place to evolve policy.

Session B addition (tasks 5-7):
:mod:`validibot.validations.services.evidence_retention` provides a
``RetentionPolicy`` consulted here. Input hashes always land in the
manifest (preimage-resistant; safe even under ``DO_NOT_STORE``);
output hashes are stripped under ``DO_NOT_STORE``. Stripped fields
are recorded in ``retention.redactions_applied`` so verifiers see
what the policy did.

Responsibilities
================

1. Walk a completed :class:`ValidationRun` and harvest the trust
   columns Phase 3 added (workflow's contract fields, validator
   semantic digests, input schema) into a structured Pydantic model.
2. Serialise that model to canonical JSON.
3. Hash the JSON bytes.
4. Persist the bytes to default storage and create the
   :class:`RunEvidenceArtifact` row pointing at them.

Failure model
=============

The builder is *best-effort*. A run completes successfully whether
or not the builder produces a manifest — the run's outcome is
independent of evidence generation. Callers wrap the builder in
:func:`stamp_evidence_manifest` (defined here), which catches every
exception, logs loudly, creates a ``RunEvidenceArtifact`` row in the
``FAILED`` state with the exception message, and returns ``None`` so
the caller can carry on.

The auditor (Phase 3 Session D's ``audit_workflow_versions`` command,
extended in this session) surfaces the gap as a
``MANIFEST_MISSING`` finding code so operators see it.

Schema location
===============

The Pydantic schema (``EvidenceManifest`` and friends) lives in
``validibot-shared`` (``validibot_shared.evidence``) so external
verifiers can depend on a single PyPI package without pulling in
the Django stack. This module is the *Django-side producer* — the
builder reads the ORM, harvests the trust columns, and constructs
a ``shared.EvidenceManifest`` instance which it then serialises +
hashes + persists.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote
from urllib.parse import urlparse

from django.core.files.base import ContentFile
from django.db import transaction
from validibot_shared.evidence import SCHEMA_VERSION
from validibot_shared.evidence import EvidenceManifest
from validibot_shared.evidence import ManifestArtifactInputBinding
from validibot_shared.evidence import ManifestArtifactLineageEdge
from validibot_shared.evidence import ManifestExecutionAttempt
from validibot_shared.evidence import ManifestExecutionInput
from validibot_shared.evidence import ManifestInputRelationship
from validibot_shared.evidence import ManifestPayloadDigests
from validibot_shared.evidence import ManifestProducedArtifact
from validibot_shared.evidence import ManifestRetentionInfo
from validibot_shared.evidence import StepValidatorRecord

from validibot.core.filesafety import sha256_hexdigest
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.services.evidence_retention import RetentionPolicy
from validibot.workflows.services.contract_snapshot import (
    build_workflow_contract_snapshot,
)

if TYPE_CHECKING:
    from validibot.validations.models import RunEvidenceArtifact
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


def _build_produced_artifact_records(
    run: ValidationRun,
) -> list[ManifestProducedArtifact]:
    """Project run artifacts into evidence-safe public metadata.

    The ``Artifact`` model has internal storage fields. This projection keeps
    stable identifiers, hashes, producer coordinates, and file descriptors while
    deliberately omitting raw storage URIs.
    """

    records: list[ManifestProducedArtifact] = []
    artifacts = run.artifacts.select_related("step_run", "workflow_step").order_by(
        "workflow_step__order", "contract_key", "item_key", "pk"
    )
    for artifact in artifacts:
        step = artifact.workflow_step
        records.append(
            ManifestProducedArtifact(
                artifact_id=str(artifact.pk),
                run_id=str(artifact.validation_run_id),
                step_run_id=str(artifact.step_run_id) if artifact.step_run_id else None,
                producer_step_id=artifact.workflow_step_id,
                producer_step_key=(step.step_key if step else ""),
                producer_step_name=(step.name if step else ""),
                contract_key=artifact.contract_key,
                item_key=artifact.item_key,
                filename=_artifact_filename(artifact),
                label=artifact.label,
                role=artifact.role,
                kind=artifact.kind,
                media_type=artifact.content_type,
                data_format=artifact.data_format,
                size_bytes=artifact.size_bytes if artifact.size_bytes >= 0 else None,
                sha256=artifact.sha256,
                storage_version=artifact.storage_version,
                manifest_sha256=artifact.manifest_sha256,
                producer_validator_type=artifact.producer_validator_type,
                producer_validator_version=artifact.producer_validator_version,
                producer_backend_image_digest=artifact.producer_backend_image_digest,
                retention_class=artifact.retention_class,
            ),
        )
    return records


def _build_artifact_input_lineage_records(
    run: ValidationRun,
) -> tuple[list[ManifestArtifactInputBinding], list[ManifestArtifactLineageEdge]]:
    """Project artifact input traces into evidence-safe binding + edge records."""

    from validibot.validations.constants import BindingSourceScope
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import ResolvedInputTrace
    from validibot.workflows.models import WorkflowStepResource

    artifact_by_id = {
        str(artifact.pk): artifact
        for artifact in run.artifacts.select_related("workflow_step", "step_run")
    }
    submission_file_by_step_port = _submission_file_index(run)
    resource_by_trace_id = _workflow_resource_index(run, WorkflowStepResource)

    bindings: list[ManifestArtifactInputBinding] = []
    edges: list[ManifestArtifactLineageEdge] = []
    traces = (
        ResolvedInputTrace.objects.filter(
            step_run__validation_run=run,
            io_definition__io_medium=StepIOMedium.ARTIFACT,
        )
        .select_related(
            "io_definition",
            "step_run",
            "step_run__workflow_step",
        )
        .order_by("step_run__step_order", "input_contract_key", "pk")
    )

    for trace in traces:
        snapshots = _trace_source_snapshots(trace)
        for snapshot in snapshots:
            binding = _artifact_input_binding_from_trace(
                run=run,
                trace=trace,
                snapshot=snapshot,
                artifact_by_id=artifact_by_id,
                submission_file_by_step_port=submission_file_by_step_port,
                resource_by_trace_id=resource_by_trace_id,
            )
            bindings.append(binding)
            if (
                binding.resolved
                and binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT
                and binding.source_artifact_id
            ):
                edges.append(
                    ManifestArtifactLineageEdge(
                        source_artifact_id=binding.source_artifact_id,
                        source_step_key=binding.producer_step_key,
                        source_contract_key=binding.producer_contract_key,
                        source_sha256=binding.source_sha256,
                        target_step_key=binding.target_step_key,
                        target_port_key=binding.target_port_key,
                        target_step_run_id=binding.target_step_run_id,
                    ),
                )

    return bindings, edges


def _artifact_input_binding_from_trace(
    *,
    run: ValidationRun,
    trace,
    snapshot,
    artifact_by_id,
    submission_file_by_step_port,
    resource_by_trace_id,
) -> ManifestArtifactInputBinding:
    """Build one binding record from a trace and one resolved source snapshot."""

    from validibot.validations.constants import BindingSourceScope

    step_run = trace.step_run
    step = step_run.workflow_step
    port = trace.io_definition
    target_port_key = trace.input_contract_key or getattr(port, "contract_key", "")
    source_scope = trace.source_scope_used or ""
    source_data = _source_details(
        run=run,
        trace=trace,
        snapshot=snapshot,
        target_step_id=step.pk,
        target_port_key=target_port_key,
        artifact_by_id=artifact_by_id,
        submission_file_by_step_port=submission_file_by_step_port,
        resource_by_trace_id=resource_by_trace_id,
    )
    if source_scope != BindingSourceScope.UPSTREAM_ARTIFACT:
        source_data.setdefault("producer_step_key", "")
        source_data.setdefault("producer_contract_key", "")

    return ManifestArtifactInputBinding(
        target_step_id=step.pk,
        target_step_key=step.step_key or "",
        target_step_name=step.name or "",
        target_step_run_id=str(step_run.pk),
        target_port_key=target_port_key,
        target_port_role=getattr(port, "role", "") if port else "",
        target_envelope_channel=getattr(port, "envelope_channel", "") if port else "",
        source_scope=source_scope,
        source_data_path=trace.source_data_path_used or "",
        resolved=bool(trace.resolved),
        used_default=bool(trace.used_default),
        error_message=trace.error_message or "",
        **source_data,
    )


def _source_details(
    *,
    run: ValidationRun,
    trace,
    snapshot,
    target_step_id: int,
    target_port_key: str,
    artifact_by_id,
    submission_file_by_step_port,
    resource_by_trace_id,
) -> dict:
    """Return evidence-safe source metadata for an artifact input trace."""

    from validibot.validations.constants import BindingSourceScope

    details = {
        "source_artifact_id": "",
        "source_submission_file_id": "",
        "source_submission_id": "",
        "source_resource_id": "",
        "source_filename": "",
        "source_media_type": "",
        "source_data_format": "",
        "source_size_bytes": None,
        "source_sha256": "",
        "source_storage_version": str(
            (snapshot or {}).get("storage_version") or "",
        ),
        "producer_step_key": "",
        "producer_contract_key": "",
    }
    if not trace.resolved:
        return details

    if trace.source_scope_used == BindingSourceScope.SUBMISSION_FILE:
        submitted_file = submission_file_by_step_port.get(
            (target_step_id, target_port_key),
        )
        if submitted_file is not None:
            details.update(
                {
                    "source_submission_file_id": str(submitted_file.pk),
                    "source_submission_id": str(submitted_file.submission_id),
                    "source_filename": submitted_file.original_filename
                    or _filename_from_uri(_snapshot_uri(snapshot)),
                    "source_media_type": submitted_file.content_type,
                    "source_size_bytes": submitted_file.size_bytes,
                    "source_sha256": submitted_file.checksum_sha256,
                },
            )
        else:
            submission = run.submission
            details.update(
                {
                    "source_submission_id": str(submission.pk) if submission else "",
                    "source_filename": (
                        submission.original_filename
                        if submission and submission.original_filename
                        else _filename_from_uri(_snapshot_uri(snapshot))
                    ),
                    "source_media_type": _submission_file_type(submission),
                    "source_size_bytes": _submission_size_bytes(submission),
                    "source_sha256": (
                        submission.checksum_sha256
                        if submission and submission.checksum_sha256
                        else ""
                    ),
                },
            )
        return details

    if trace.source_scope_used == BindingSourceScope.WORKFLOW_RESOURCE:
        resource_id = str((snapshot or {}).get("id") or "")
        resource = resource_by_trace_id.get(resource_id)
        details["source_resource_id"] = resource_id
        if resource is None:
            details.update(
                {
                    "source_filename": _filename_from_uri(_snapshot_uri(snapshot)),
                    "source_data_format": str((snapshot or {}).get("type") or ""),
                },
            )
            return details

        if resource.is_catalog_reference:
            catalog = resource.validator_resource_file
            details.update(
                {
                    "source_filename": catalog.filename,
                    "source_data_format": catalog.resource_type,
                    "source_sha256": catalog.content_hash,
                },
            )
        else:
            details.update(
                {
                    "source_filename": resource.filename,
                    "source_data_format": resource.resource_type,
                    "source_sha256": resource.content_hash,
                },
            )
        return details

    if trace.source_scope_used == BindingSourceScope.UPSTREAM_ARTIFACT:
        artifact_ref = (snapshot or {}).get("artifact") or {}
        artifact_id = str(artifact_ref.get("artifact_id") or "")
        artifact = artifact_by_id.get(artifact_id)
        details["source_artifact_id"] = artifact_id
        if artifact is not None:
            step = artifact.workflow_step
            details.update(
                {
                    "source_filename": _artifact_filename(artifact),
                    "source_media_type": artifact.content_type,
                    "source_data_format": artifact.data_format,
                    "source_size_bytes": (
                        artifact.size_bytes if artifact.size_bytes >= 0 else None
                    ),
                    "source_sha256": artifact.sha256,
                    "source_storage_version": artifact.storage_version,
                    "producer_step_key": step.step_key if step else "",
                    "producer_contract_key": artifact.contract_key,
                },
            )
        else:
            details.update(
                {
                    "source_filename": str(artifact_ref.get("filename") or ""),
                    "source_media_type": str(artifact_ref.get("media_type") or ""),
                    "source_data_format": str(artifact_ref.get("data_format") or ""),
                    "source_size_bytes": artifact_ref.get("size_bytes"),
                    "source_sha256": str(artifact_ref.get("sha256") or ""),
                    "source_storage_version": str(
                        artifact_ref.get("storage_version") or "",
                    ),
                    "producer_step_key": str(
                        artifact_ref.get("producer_step_key") or "",
                    ),
                    "producer_contract_key": str(
                        artifact_ref.get("contract_key") or "",
                    ),
                },
            )
        return details

    return details


def _submission_file_type(submission) -> str:
    """Return the primary submission's file type without branching in dicts."""

    return submission.file_type if submission and submission.file_type else ""


def _submission_size_bytes(submission) -> int | None:
    """Return the primary submission size when it was retained as metadata."""

    if not submission or not submission.size_bytes:
        return None
    return submission.size_bytes


def _submission_file_index(run: ValidationRun) -> dict[tuple[int, str], object]:
    """Return submitted artifact-port files keyed by consumer step + port."""

    if not run.submission_id:
        return {}
    return {
        (item.workflow_step_id, item.port_key): item
        for item in run.submission.input_files.all()
        if item.workflow_step_id and item.port_key
    }


def _workflow_resource_index(run: ValidationRun, workflow_resource_model) -> dict:
    """Return workflow resources keyed by the IDs runtime traces use."""

    index: dict[str, object] = {}
    resources = workflow_resource_model.objects.filter(
        step__workflow=run.workflow,
    ).select_related("validator_resource_file", "step")
    for resource in resources:
        index[str(resource.pk)] = resource
        if resource.validator_resource_file_id:
            index[str(resource.validator_resource_file_id)] = resource
    return index


def _trace_source_snapshots(trace) -> list[dict | None]:
    """Return one snapshot per materialized source for a trace."""

    snapshot = trace.value_snapshot
    if isinstance(snapshot, list):
        return snapshot or [None]
    if isinstance(snapshot, dict):
        return [snapshot]
    return [None]


def _artifact_filename(artifact) -> str:
    """Return an artifact filename without exposing its storage URI."""

    uri_or_name = artifact.storage_uri or getattr(artifact.file, "name", "")
    return _filename_from_uri(uri_or_name) or artifact.label


def _snapshot_uri(snapshot) -> str:
    """Read a private runtime URI from a trace snapshot without exporting it."""

    if not isinstance(snapshot, dict):
        return ""
    return str(snapshot.get("uri") or "")


def _filename_from_uri(uri: str) -> str:
    """Return a URI/path basename for evidence-safe display."""

    if not uri:
        return ""
    parsed = urlparse(uri)
    path = parsed.path if parsed.scheme else uri
    return Path(unquote(path)).name


def _build_execution_attempt_records(
    run: ValidationRun,
    *,
    include_output_hash: bool,
) -> list[ManifestExecutionAttempt]:
    """Project committed attempt snapshots into portable execution evidence.

    Attempts created before the launch-time snapshot shipped are omitted. Their
    digest alone cannot reconstruct verified per-file identities without
    rereading a storage URI, which this evidence path deliberately refuses to do.
    """
    from validibot.validations.models import ExecutionAttempt

    records: list[ManifestExecutionAttempt] = []
    attempts = (
        ExecutionAttempt.objects.filter(step_run__validation_run=run)
        .exclude(input_envelope_sha256="")
        .select_related("step_run")
        .order_by("step_run__step_order", "attempt_number", "pk")
    )
    for attempt in attempts:
        snapshot = attempt.input_evidence_snapshot
        if not isinstance(snapshot, dict):
            continue
        attempt_contract_version = str(
            snapshot.get("attempt_contract_version") or "",
        )
        raw_input_files = snapshot.get("input_files")
        raw_relationships = snapshot.get("input_relationships")
        if (
            not attempt_contract_version
            or not isinstance(raw_input_files, list)
            or not isinstance(raw_relationships, list)
        ):
            continue

        input_files = [
            ManifestExecutionInput.model_validate(item) for item in raw_input_files
        ]
        input_relationships = [
            ManifestInputRelationship.model_validate(item) for item in raw_relationships
        ]
        inputs_verified = attempt.state == ExecutionAttemptState.COMPLETED and bool(
            attempt.output_envelope_sha256
        )
        records.append(
            ManifestExecutionAttempt(
                execution_attempt_id=str(attempt.pk),
                step_run_id=str(attempt.step_run_id),
                attempt_number=attempt.attempt_number,
                state=str(attempt.state),
                runner_type=attempt.runner_type,
                provider_execution_id=attempt.provider_execution_id,
                attempt_contract_version=attempt_contract_version,
                input_envelope_sha256=attempt.input_envelope_sha256,
                output_envelope_sha256=(
                    attempt.output_envelope_sha256
                    if include_output_hash and attempt.output_envelope_sha256
                    else None
                ),
                backend_image_digest=(
                    attempt.backend_image_digest
                    or attempt.step_run.validator_backend_image_digest
                ),
                inputs_verified=inputs_verified,
                input_files=input_files,
                input_relationships=input_relationships,
            ),
        )
    return records


class EvidenceManifestBuilder:
    """Stateless builder for run-level evidence manifests.

    Static methods only — see ``WorkflowVersioningService`` for the
    rationale on class-vs-free-functions in this codebase.
    """

    @staticmethod
    def build(run: ValidationRun) -> EvidenceManifest:
        """Harvest a completed run into a validated :class:`EvidenceManifest`.

        Pure read operation — does not touch the database except via
        the ORM relationships already loaded (or auto-fetched via the
        run's ``select_related`` / ``prefetch_related`` chains).

        Args:
            run: The completed ``ValidationRun`` to document.
                Caller is expected to have refreshed the row so
                ``status``, ``ended_at``, and the workflow snapshot
                are stable.

        Returns:
            A frozen ``EvidenceManifest`` Pydantic instance. Caller
            can serialise via ``model_dump_json(...)`` or pass to
            ``persist`` to also write to storage.
        """
        workflow = run.workflow
        # Single source of the contract snapshot (ADR-2026-06-18): the community
        # projection carries the launch contract PLUS constants, signal-mapping
        # definitions, and the workflow-definition hash — the same object the Pro
        # signed credential derives from, so a constant change moves both.
        contract = build_workflow_contract_snapshot(workflow)

        # Walk steps in execution order so the manifest's record
        # mirrors what the run actually did. Pre-fetch the validator
        # so we don't generate N+1 queries here.
        steps_qs = workflow.steps.select_related("validator").order_by("order")

        # Trust ADR Phase 5 Session A — per-step validator backend
        # image digest, sourced from the ``ValidationStepRun`` rows
        # the runner persisted at execution / launch time. We index
        # by ``workflow_step_id`` so the workflow-step walk below
        # can attach the matching digest in O(1) without N+1
        # queries. Async (Cloud Run) runs persisted the digest at
        # launch time; sync (Docker) runs persisted it at finalize
        # via the validator's stats bag. Either way, by the time
        # we're building a manifest, ``validator_backend_image_digest``
        # is the authoritative column.
        step_run_digests: dict[int, str] = {
            step_run.workflow_step_id: (step_run.validator_backend_image_digest or "")
            for step_run in run.step_runs.all()
        }

        step_records: list[StepValidatorRecord] = []
        for step in steps_qs:
            validator = step.validator
            if validator is None:
                # Steps without a validator (e.g. action-only steps)
                # contribute nothing to the validator-trust story.
                # Skip rather than emit an empty record.
                continue
            backend_digest = step_run_digests.get(step.pk, "")
            step_records.append(
                StepValidatorRecord(
                    step_id=step.pk,
                    step_order=step.order,
                    validator_slug=validator.slug,
                    validator_version=str(validator.version),
                    validator_semantic_digest=(validator.semantic_digest or None),
                    validator_backend_image_digest=(backend_digest or None),
                ),
            )

        # Phase 4 Session B: populate retention info + payload digests.
        # The retention policy decides what's safe per retention class.
        # ``redactions_applied`` is the externally-visible audit trail
        # of "the policy stripped these fields" so verifiers know what
        # to expect in the manifest's shape.
        retention_class = workflow.input_retention or ""
        retention = ManifestRetentionInfo(
            retention_class=retention_class,
            redactions_applied=RetentionPolicy.redactions_for(retention_class),
        )

        # Input hash is always populated (preimage-resistant; doesn't
        # leak DO_NOT_STORE bytes). Output hash is gated by retention.
        # Both fields are sourced from columns already populated at
        # run time: Submission.checksum_sha256 and ValidationRun.output_hash.
        input_hash = ""
        if run.submission and run.submission.checksum_sha256:
            input_hash = run.submission.checksum_sha256

        output_hash = ""
        if RetentionPolicy.includes_output_hash(retention_class):
            output_hash = run.output_hash or ""

        payload_digests = ManifestPayloadDigests(
            input_sha256=input_hash or None,
            output_envelope_sha256=output_hash or None,
        )
        produced_artifacts = _build_produced_artifact_records(run)
        artifact_input_bindings, artifact_lineage_edges = (
            _build_artifact_input_lineage_records(run)
        )
        execution_attempts = _build_execution_attempt_records(
            run,
            include_output_hash=RetentionPolicy.includes_output_hash(
                retention_class,
            ),
        )

        # The schema's ``executed_at`` is a string (ISO 8601 UTC) so
        # the canonical-JSON byte sequence is stable across Python
        # versions and serialisation backends. ``run.ended_at`` is
        # already a tz-aware datetime in production; convert to ISO
        # string here. ``isoformat()`` on a tz-aware datetime
        # produces the offset-suffixed form Pydantic round-trips
        # cleanly.
        executed_at_iso = run.ended_at.isoformat() if run.ended_at is not None else ""

        # Record the run's source in the manifest.  ``run.source`` is
        # populated by the launch path from the *authenticated route*
        # (never from a client-controlled header).
        # ``ValidationRunSource`` is a ``TextChoices`` enum so the
        # column value is already a string; the manifest stores the
        # raw enum value (e.g. ``"X402_AGENT"``) so external verifiers
        # can compare it without importing the Django enum.
        #
        # Forward-compat shim: emit ``source=`` only when the installed
        # ``validibot-shared`` schema accepts the field.  The field was
        # added in shared 0.7.4; the lockfile bump to 0.7.4 (or later)
        # activates population.  Without this guard, a transitional
        # build pinned to 0.7.3 would raise on ``source=``.
        manifest_kwargs: dict = {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(run.id),
            "workflow_id": workflow.pk,
            "workflow_slug": workflow.slug,
            # Evidence manifests use the external validibot-shared v1 schema,
            # where workflow_version is a string identifier. The authoritative
            # Django model stores workflow versions as positive integers; cast
            # at this boundary so canonical evidence JSON stays schema-valid.
            "workflow_version": str(workflow.version),
            "org_id": run.org_id,
            "executed_at": executed_at_iso,
            "status": str(run.status),
            "workflow_contract": contract,
            "steps": step_records,
            "input_schema": workflow.input_schema or None,
            "retention": retention,
            "payload_digests": payload_digests,
            "execution_attempts": execution_attempts,
        }
        if "source" in EvidenceManifest.model_fields:
            manifest_kwargs["source"] = (run.source or None) or None
        if "produced_artifacts" in EvidenceManifest.model_fields:
            manifest_kwargs["produced_artifacts"] = produced_artifacts
        if "artifact_input_bindings" in EvidenceManifest.model_fields:
            manifest_kwargs["artifact_input_bindings"] = artifact_input_bindings
        if "artifact_lineage_edges" in EvidenceManifest.model_fields:
            manifest_kwargs["artifact_lineage_edges"] = artifact_lineage_edges

        return EvidenceManifest(**manifest_kwargs)

    @staticmethod
    def serialise(manifest: EvidenceManifest) -> bytes:
        """Return the canonical-JSON bytes of ``manifest``.

        The serialisation is the contract the hash is computed over:

        - ``sort_keys=True`` so verifiers regenerating the bytes
          from a parsed manifest get the same byte sequence.
        - ``separators=(",", ":")`` to strip whitespace; same hash
          regardless of indentation choices.
        - ``ensure_ascii=True`` so the byte sequence is stable
          across Python locales.
        """
        # Pydantic's model_dump() produces a dict that we then
        # canonicalise via json.dumps. Going through json.dumps
        # rather than model_dump_json gives us the sort_keys +
        # separators control we need.
        as_dict = manifest.model_dump(mode="json")
        return json.dumps(
            as_dict,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    @staticmethod
    @transaction.atomic
    def persist(
        run: ValidationRun,
        manifest: EvidenceManifest,
    ) -> RunEvidenceArtifact:
        """Serialise + hash + store the manifest, returning the new artifact.

        Atomic: the file write and the DB row insert succeed
        together or the transaction rolls back. A failure here
        leaves the run un-stamped; the caller's wrapper records the
        gap.

        If a previous attempt left a ``FAILED`` artifact for this
        run, this method updates it in place (lifting it to
        ``GENERATED``) rather than crashing on the
        ``OneToOneField`` uniqueness constraint.
        """
        # Local import to avoid the circular models<->services chain.
        from validibot.validations.models import RunEvidenceArtifact
        from validibot.validations.models import RunEvidenceArtifactAvailability

        manifest_bytes = EvidenceManifestBuilder.serialise(manifest)
        manifest_hash = sha256_hexdigest(manifest_bytes)

        artifact, _ = RunEvidenceArtifact.objects.get_or_create(
            run=run,
            defaults={
                "schema_version": manifest.schema_version,
                "manifest_hash": manifest_hash,
                "retention_class": manifest.retention.retention_class,
                "availability": RunEvidenceArtifactAvailability.GENERATED,
            },
        )

        # Whether we created it or it already existed (from a
        # FAILED prior attempt), set the manifest fields to the
        # current state.
        artifact.schema_version = manifest.schema_version
        artifact.manifest_hash = manifest_hash
        artifact.retention_class = manifest.retention.retention_class
        artifact.availability = RunEvidenceArtifactAvailability.GENERATED
        artifact.generation_error = ""
        artifact.manifest_path.save(
            "manifest.json",
            ContentFile(manifest_bytes),
            save=False,
        )
        artifact.save()
        return artifact


def stamp_evidence_manifest(run: ValidationRun) -> RunEvidenceArtifact | None:
    """Best-effort manifest stamp called from run-completion hooks.

    Wraps :meth:`EvidenceManifestBuilder.build` +
    :meth:`EvidenceManifestBuilder.persist` in a try/except. On
    success, returns the new ``RunEvidenceArtifact`` row. On any
    failure, logs the exception, creates (or updates) a ``FAILED``
    artifact row recording the error message, and returns ``None``.

    The function NEVER raises — callers can assume run-completion
    state isn't affected by manifest generation.
    """
    # Local imports avoid circular dependency at module load.
    from validibot.validations.models import RunEvidenceArtifact
    from validibot.validations.models import RunEvidenceArtifactAvailability

    try:
        manifest = EvidenceManifestBuilder.build(run)
        return EvidenceManifestBuilder.persist(run, manifest)
    except Exception as exc:
        logger.exception(
            "Evidence manifest generation failed for run %s",
            run.id,
        )
        try:
            artifact, _ = RunEvidenceArtifact.objects.get_or_create(
                run=run,
                defaults={
                    "schema_version": SCHEMA_VERSION,
                    "availability": RunEvidenceArtifactAvailability.FAILED,
                    "generation_error": str(exc)[:5000],
                },
            )
            artifact.availability = RunEvidenceArtifactAvailability.FAILED
            artifact.generation_error = str(exc)[:5000]
            artifact.save(
                update_fields=["availability", "generation_error", "modified"],
            )
        except Exception:
            logger.exception(
                "Could not even record manifest-generation failure for run %s",
                run.id,
            )
        return None


__all__ = [
    "EvidenceManifestBuilder",
    "stamp_evidence_manifest",
]
