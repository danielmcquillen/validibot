"""``EvidenceManifestBuilder`` — build + persist evidence manifests for runs.

ADR-2026-04-27 Phase 4 Session A (tasks 3-4): the single decision
point for "what goes in a manifest, in what shape." Mirrors the
service-class pattern from
:class:`validibot.workflows.services.versioning.WorkflowVersioningService`:
stateless, static methods, central place to evolve policy.

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
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction
from validibot_shared.evidence import SCHEMA_VERSION
from validibot_shared.evidence import EvidenceManifest
from validibot_shared.evidence import ManifestRetentionInfo
from validibot_shared.evidence import StepValidatorRecord
from validibot_shared.evidence import WorkflowContractSnapshot

from validibot.core.filesafety import sha256_hexdigest

if TYPE_CHECKING:
    from validibot.validations.models import RunEvidenceArtifact
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


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
        contract = WorkflowContractSnapshot(
            allowed_file_types=list(workflow.allowed_file_types or []),
            data_retention=workflow.data_retention or "",
            output_retention=workflow.output_retention or "",
            agent_billing_mode=workflow.agent_billing_mode or "",
            agent_price_cents=workflow.agent_price_cents,
            agent_max_launches_per_hour=workflow.agent_max_launches_per_hour,
            agent_public_discovery=workflow.agent_public_discovery,
            agent_access_enabled=workflow.agent_access_enabled,
        )

        # Walk steps in execution order so the manifest's record
        # mirrors what the run actually did. Pre-fetch the validator
        # so we don't generate N+1 queries here.
        steps_qs = workflow.steps.select_related("validator").order_by("order")
        step_records: list[StepValidatorRecord] = []
        for step in steps_qs:
            validator = step.validator
            if validator is None:
                # Steps without a validator (e.g. action-only steps)
                # contribute nothing to the validator-trust story.
                # Skip rather than emit an empty record.
                continue
            step_records.append(
                StepValidatorRecord(
                    step_id=step.pk,
                    step_order=step.order,
                    validator_slug=validator.slug,
                    validator_version=validator.version,
                    validator_semantic_digest=(validator.semantic_digest or None),
                ),
            )

        retention = ManifestRetentionInfo(
            retention_class=workflow.data_retention or "",
            redactions_applied=[],  # Session B fills this
        )

        # The schema's ``executed_at`` is a string (ISO 8601 UTC) so
        # the canonical-JSON byte sequence is stable across Python
        # versions and serialisation backends. ``run.ended_at`` is
        # already a tz-aware datetime in production; convert to ISO
        # string here. ``isoformat()`` on a tz-aware datetime
        # produces the offset-suffixed form Pydantic round-trips
        # cleanly.
        executed_at_iso = run.ended_at.isoformat() if run.ended_at is not None else ""

        return EvidenceManifest(
            schema_version=SCHEMA_VERSION,
            run_id=str(run.id),
            workflow_id=workflow.pk,
            workflow_slug=workflow.slug,
            workflow_version=workflow.version,
            org_id=run.org_id,
            executed_at=executed_at_iso,
            status=str(run.status),
            workflow_contract=contract,
            steps=step_records,
            input_schema=workflow.input_schema or None,
            retention=retention,
            # payload_digests defaults to all-None in Session A.
        )

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
