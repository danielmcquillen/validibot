"""Build and persist the minimal permanent evidence receipt.

The manifest is intentionally narrower than the run model. It records only
the run outcome, the workflow version and validation steps that ran, and the
canonical input/output digests. It never copies raw payloads, workflow
configuration, retention policy, filenames, provider details, or artifact
lineage into permanent evidence. The one execution-environment fact retained
is the captured backend image digest on a workflow step.

The builder is best-effort: a manifest-generation failure is recorded on the
``RunEvidenceArtifact`` row and never changes the run's outcome.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction
from validibot_shared.evidence import SCHEMA_VERSION
from validibot_shared.evidence import EvidenceManifest
from validibot_shared.evidence import WorkflowReceipt
from validibot_shared.evidence import WorkflowStepReceipt

from validibot.core.filesafety import sha256_hexdigest

if TYPE_CHECKING:
    from validibot.validations.models import RunEvidenceArtifact
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


def _choice_value(value) -> str:
    """Return a Django choice's stored value without importing its enum."""

    return str(getattr(value, "value", value) or "")


class EvidenceManifestBuilder:
    """Build and persist one small, credential-bound evidence manifest."""

    @staticmethod
    def build(run: ValidationRun) -> EvidenceManifest:
        """Build the permanent receipt for a completed validation run."""

        if run.ended_at is None:
            raise ValueError("Cannot build an evidence manifest before run completion")

        workflow = run.workflow
        step_runs = {
            step_run.workflow_step_id: step_run for step_run in run.step_runs.all()
        }
        steps: list[WorkflowStepReceipt] = []
        for step in workflow.steps.select_related("validator").order_by("order"):
            if step.validator is None:
                # Action-only steps do not contribute validation identity to the
                # minimal receipt. The credential still records the run outcome.
                continue
            step_run = step_runs.get(step.pk)
            steps.append(
                WorkflowStepReceipt(
                    key=step.step_key or str(step.pk),
                    status=(
                        _choice_value(step_run.status)
                        if step_run is not None
                        else "NOT_RUN"
                    ),
                    validator=step.validator.slug,
                    validator_version=str(step.validator.version),
                    backend_image_digest=(
                        step_run.validator_backend_image_digest or None
                        if step_run is not None
                        else None
                    ),
                ),
            )

        input_sha256 = None
        if run.submission and run.submission.checksum_sha256:
            input_sha256 = run.submission.checksum_sha256

        # The digest is captured before payload retention purges the output.
        # Retention controls the bytes, not this permanent receipt metadata.
        output_envelope_sha256 = run.output_hash or None

        return EvidenceManifest(
            run_id=str(run.id),
            completed_at=run.ended_at.isoformat(),
            status=_choice_value(run.status),
            workflow=WorkflowReceipt(
                slug=workflow.slug,
                version=str(workflow.version),
                steps=steps,
            ),
            input_sha256=input_sha256,
            output_envelope_sha256=output_envelope_sha256,
        )

    @staticmethod
    def serialise(manifest: EvidenceManifest) -> bytes:
        """Return canonical JSON bytes for hashing and persistence."""

        as_dict = manifest.model_dump(mode="json", by_alias=True)
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
        """Serialise, hash, and store a manifest for ``run``."""

        from validibot.validations.models import RunEvidenceArtifact
        from validibot.validations.models import RunEvidenceArtifactAvailability

        manifest_bytes = EvidenceManifestBuilder.serialise(manifest)
        manifest_hash = sha256_hexdigest(manifest_bytes)
        artifact, _ = RunEvidenceArtifact.objects.get_or_create(
            run=run,
            defaults={
                "schema_version": manifest.schema_url,
                "manifest_hash": manifest_hash,
                "availability": RunEvidenceArtifactAvailability.GENERATED,
            },
        )

        artifact.schema_version = manifest.schema_url
        artifact.manifest_hash = manifest_hash
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
    """Best-effort manifest stamp called from run-completion hooks."""

    from validibot.validations.models import RunEvidenceArtifact
    from validibot.validations.models import RunEvidenceArtifactAvailability

    try:
        manifest = EvidenceManifestBuilder.build(run)
        return EvidenceManifestBuilder.persist(run, manifest)
    except Exception as exc:
        logger.exception("Evidence manifest generation failed for run %s", run.id)
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
