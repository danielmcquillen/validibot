"""Tests for validator backend image digest capture.

ADR-2026-04-27 Phase 5 Session A — every advanced-validator step run
records the resolved sha256 digest of the container image that
actually executed, so an evidence claim can answer "what *exact* code
ran?" beyond name/version.

This suite exercises the four moving parts of digest capture:

1. The Docker runner reads ``container.image.attrs["RepoDigests"]``
   (registry-anchored, verifiable) when available, and falls back to
   ``container.attrs["Image"]`` (local image ID) for dev images that
   were never pulled from a registry. The helper
   ``_resolve_container_image_digest`` encapsulates the preference
   order and the defensive ``None`` return on inspection failures —
   digest capture must never break a run.
2. The Cloud Run path's ``get_execution_image_digest`` resolves the
   image reference from the Execution resource's
   ``template.containers[0].image`` field. Best-effort, returning
   ``None`` on any failure mode.
3. ``_finalize_step_run`` (sync orchestrator path) promotes the
   digest from the validator's ``stats`` bag onto the typed
   ``ValidationStepRun.validator_backend_image_digest`` column so it
   survives independently of the JSON output blob.
4. The evidence manifest builder threads the per-step digest into
   each ``StepValidatorRecord``, looking up the
   ``ValidationStepRun`` by ``workflow_step_id`` so simple-validator
   steps (which leave the column empty) emit ``None`` for the field.

What's deliberately not covered here
====================================

- A full integration test against a real Docker daemon — the unit
  layer asserts the runner inspects the SDK objects correctly; the
  daemon enforces what we set. A real-image smoke test belongs in
  the manual verification flow.
- Cosign verification (Session A.2) — that lives in a separate test
  file alongside the cosign helper.
- Session B's policy gate — these tests assume *capture* only;
  enforcement of "must be a digest" is a Session B concern.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.services.runners.docker import (
    _resolve_container_image_digest,
)
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_fake_container(*, repo_digests=None, local_image_id=None):
    """Build a mock Docker container exposing the inspection surfaces.

    Mirrors the shape the docker-py SDK returns from
    ``client.containers.run()``. We populate just enough for the
    helper to walk: ``container.image.attrs["RepoDigests"]`` and
    ``container.attrs["Image"]``.
    """
    container = MagicMock()
    if repo_digests is not None:
        container.image.attrs = {"RepoDigests": list(repo_digests)}
    else:
        # Default to no RepoDigests so the helper falls back.
        container.image.attrs = {}
    if local_image_id is not None:
        container.attrs = {"Image": local_image_id}
    else:
        container.attrs = {}
    return container


# ── _resolve_container_image_digest helper ──────────────────────────────


class TestResolveContainerImageDigest:
    """Direct unit tests for the helper that the Docker runner uses."""

    def test_prefers_repo_digests_when_available(self):
        """Registry-anchored references win over local image IDs.

        A verifier can ``docker pull`` a ``registry/...@sha256:...``
        reference and confirm bit-for-bit equivalence; a bare local
        ``sha256:...`` ID is only a content fingerprint and isn't
        independently re-pullable. The helper must prefer the
        verifiable form when the SDK exposes it.
        """
        container = _make_fake_container(
            repo_digests=["registry.example/validator@sha256:" + "a" * 64],
            local_image_id="sha256:" + "b" * 64,
        )
        digest = _resolve_container_image_digest(container)
        assert digest == "registry.example/validator@sha256:" + "a" * 64

    def test_falls_back_to_local_image_id_when_no_repo_digests(self):
        """Local-built dev images without a registry-anchored tag.

        Docker-py only populates ``RepoDigests`` for images pulled
        from a registry. Locally-built dev images (e.g. those
        produced by ``docker build`` against a ``Dockerfile`` in the
        validator backend repo during development) have no
        ``RepoDigests`` entry. The local image ID is still a valid
        sha256 content fingerprint — useful for matching dev-image
        bytes even though it's not re-pullable.
        """
        container = _make_fake_container(
            repo_digests=[],  # empty list, not missing key
            local_image_id="sha256:" + "c" * 64,
        )
        digest = _resolve_container_image_digest(container)
        assert digest == "sha256:" + "c" * 64

    def test_returns_none_when_nothing_available(self):
        """Both inspection paths empty → ``None`` (capture is best-effort)."""
        container = _make_fake_container()
        digest = _resolve_container_image_digest(container)
        assert digest is None

    def test_returns_none_when_inspection_raises(self):
        """SDK raising on attribute access doesn't break the run.

        The contract is: digest capture is observational, never
        enforcing. If the Docker SDK changes shape or the inspection
        objects raise, the runner should log at debug and return
        ``None`` rather than abort the run.
        """

        class _ExplodingContainer:
            """Minimal stub whose attribute access always raises.

            ``MagicMock`` won't propagate exceptions through
            attribute access (``side_effect`` only fires on call),
            so we use a hand-rolled class. This mirrors the
            pathological case where the docker-py SDK changes shape
            under our feet.
            """

            @property
            def image(self):
                msg = "image attribute boom"
                raise RuntimeError(msg)

            @property
            def attrs(self):
                msg = "attrs attribute boom"
                raise RuntimeError(msg)

        digest = _resolve_container_image_digest(_ExplodingContainer())
        assert digest is None

    def test_uses_first_repo_digest_when_multiple(self):
        """Multiple registry tags → first wins (any one is valid).

        Docker can list multiple ``RepoDigests`` when an image was
        tagged into several registries with the same content. Any
        single entry is a cryptographically valid reference; we use
        the first deterministically so two captures of the same
        container yield the same recorded value.
        """
        container = _make_fake_container(
            repo_digests=[
                "registry-a.example/validator@sha256:" + "1" * 64,
                "registry-b.example/validator@sha256:" + "1" * 64,
            ],
        )
        digest = _resolve_container_image_digest(container)
        assert digest == "registry-a.example/validator@sha256:" + "1" * 64


# ── Manifest builder propagation ────────────────────────────────────────


class TestManifestIncludesBackendImageDigest:
    """The evidence manifest exposes the per-step backend image digest."""

    def test_populates_record_when_step_run_has_digest(self):
        """Captured digest appears on the corresponding StepValidatorRecord.

        Confirms the join between ``WorkflowStep`` and
        ``ValidationStepRun`` happens correctly: the builder walks
        ``workflow.steps`` for record identity, then looks up the
        run-time digest from ``run.step_runs.all()`` keyed by
        ``workflow_step_id``.
        """
        from validibot.validations.models import ValidationStepRun

        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        step = WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        captured_digest = "registry.example/backend@sha256:" + "f" * 64
        ValidationStepRun.objects.create(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
            validator_backend_image_digest=captured_digest,
        )

        manifest = EvidenceManifestBuilder.build(run)
        assert len(manifest.steps) == 1
        record = manifest.steps[0]
        assert record.validator_backend_image_digest == captured_digest

    def test_record_is_none_when_no_digest_captured(self):
        """Simple-validator (or unbacked) steps emit ``None`` for the field.

        Steps without a backend container (simple validators that
        run inline in the Django process, or steps captured before
        digest capture shipped) leave
        ``ValidationStepRun.validator_backend_image_digest`` empty.
        The manifest must surface that absence as ``None`` so
        downstream verifiers know the gap is structural, not data
        loss.
        """
        from validibot.validations.models import ValidationStepRun

        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        step = WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        # Step run with the column at its default empty string.
        ValidationStepRun.objects.create(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
        )

        manifest = EvidenceManifestBuilder.build(run)
        assert len(manifest.steps) == 1
        assert manifest.steps[0].validator_backend_image_digest is None

    def test_record_is_none_when_no_step_run_exists(self):
        """No step run row → no digest to attach.

        Defensive case: the manifest builder is called for a run
        whose step rows haven't been materialised yet (or were
        manually deleted). The builder must not crash; it emits a
        record with ``validator_backend_image_digest=None``.
        """
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )

        manifest = EvidenceManifestBuilder.build(run)
        assert len(manifest.steps) == 1
        assert manifest.steps[0].validator_backend_image_digest is None


# ── _finalize_step_run digest persistence ───────────────────────────────


class TestFinalizeStepRunPersistsDigest:
    """The orchestrator promotes digest-from-stats onto the typed column.

    The Docker (sync) path threads the digest through the validator's
    ``stats`` bag because that's the existing channel for runner
    metadata to reach the orchestrator. ``_finalize_step_run`` is
    where that bag becomes durable database state. The trust column
    must travel out of the bag and into its dedicated column so
    auditors can query it without parsing JSON.
    """

    def _make_orchestrator_and_step_run(self):
        """Build a stateless ``StepOrchestrator`` and a fresh ``ValidationStepRun``."""
        from validibot.validations.models import ValidationStepRun
        from validibot.validations.services.step_orchestrator import StepOrchestrator

        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        step = WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.RUNNING,
        )
        step_run = ValidationStepRun.objects.create(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.RUNNING,
        )
        # The orchestrator is stateless — its methods operate on the
        # arguments they receive rather than instance state.
        orchestrator = StepOrchestrator()
        return orchestrator, step_run

    def test_writes_digest_from_stats_to_typed_column(self):
        """Stats bag carrying the digest → column populated on save."""
        orchestrator, step_run = self._make_orchestrator_and_step_run()
        captured_digest = "registry.example/backend@sha256:" + "9" * 64
        stats = {"validator_backend_image_digest": captured_digest}

        orchestrator._finalize_step_run(
            step_run=step_run,
            status=StepStatus.PASSED,
            stats=stats,
        )

        step_run.refresh_from_db()
        assert step_run.validator_backend_image_digest == captured_digest

    def test_leaves_column_empty_when_stats_missing_digest(self):
        """No digest in stats → typed column stays empty (no clobber).

        Important for the async / Cloud Run path: the launcher
        writes the digest at launch time and the callback's stats
        bag never carries it. Finalize must NOT overwrite an
        already-populated column with empty.
        """
        orchestrator, step_run = self._make_orchestrator_and_step_run()
        # Pre-populate the column to mimic the Cloud Run launch-time
        # write that happens before the callback's finalize.
        prelaunch_digest = "registry.example/backend@sha256:" + "a" * 64
        step_run.validator_backend_image_digest = prelaunch_digest
        step_run.save(update_fields=["validator_backend_image_digest"])

        # Stats bag without the digest field — finalize must leave
        # the existing column value alone.
        orchestrator._finalize_step_run(
            step_run=step_run,
            status=StepStatus.PASSED,
            stats={"some_other_metric": 42},
        )

        step_run.refresh_from_db()
        assert step_run.validator_backend_image_digest == prelaunch_digest


# ── _mark_step_run_running launch-time write ────────────────────────────


class TestMarkStepRunRunningPersistsDigest:
    """The Cloud Run launcher writes the digest at launch time.

    Cloud Run jobs are async — the digest is available the moment
    the Execution resource exists, well before the validator
    callback fires. Writing at launch time means the trust column
    is populated even if the run later crashes mid-execution. The
    finalize-time persistence is a no-op for this path because
    stats never carries the digest in async flow.
    """

    def test_writes_digest_when_provided(self):
        """``image_digest=...`` lands on the typed column."""
        from validibot.validations.models import ValidationStepRun
        from validibot.validations.services.cloud_run.launcher import (
            _mark_step_run_running,
        )

        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        step = WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.RUNNING,
        )
        step_run = ValidationStepRun.objects.create(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PENDING,
        )

        digest = "gcr.io/example/backend@sha256:" + "5" * 64
        _mark_step_run_running(step_run, image_digest=digest)

        step_run.refresh_from_db()
        assert step_run.validator_backend_image_digest == digest
        assert step_run.status == StepStatus.RUNNING

    def test_leaves_digest_empty_when_unresolved(self):
        """``image_digest=None`` → column stays at its default empty string.

        Honest absence: when the Execution metadata lookup fails,
        we don't synthesise a fake digest — the empty column tells
        an auditor "the runner couldn't resolve a digest for this
        run." Better than a misleading guess.
        """
        from validibot.validations.models import ValidationStepRun
        from validibot.validations.services.cloud_run.launcher import (
            _mark_step_run_running,
        )

        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        step = WorkflowStepFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            status=ValidationRunStatus.RUNNING,
        )
        step_run = ValidationStepRun.objects.create(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PENDING,
        )

        _mark_step_run_running(step_run, image_digest=None)

        step_run.refresh_from_db()
        assert step_run.validator_backend_image_digest == ""
        assert step_run.status == StepStatus.RUNNING
