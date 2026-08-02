"""Tests for the boundary between payload retention and permanent evidence.

Input/output retention deletes payload bytes. It does not select manifest
fields, redact the receipt, or remove its hashes. These tests protect that
simple boundary and ensure retention changes do not create multiple manifest
shapes.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _completed_run(*, output_retention: str, input_hash: str, output_hash: str):
    """Build equivalent runs that differ only in payload retention policy."""

    workflow = WorkflowFactory(
        input_retention=SubmissionRetention.DO_NOT_STORE,
        output_retention=output_retention,
    )
    WorkflowStepFactory(workflow=workflow)
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=workflow.user,
        checksum_sha256=input_hash,
    )
    return ValidationRunFactory(
        workflow=workflow,
        submission=submission,
        status=ValidationRunStatus.SUCCEEDED,
        ended_at=datetime(2026, 5, 2, tzinfo=UTC),
        output_hash=output_hash,
        output_retention_policy=output_retention,
    )


class TestPermanentReceiptRetentionBoundary:
    """Retention controls bytes, not the permanent manifest shape."""

    def test_do_not_store_keeps_both_payload_digests(self):
        """Deleted payloads remain identifiable without retaining their bytes."""

        run = _completed_run(
            output_retention=OutputRetention.DO_NOT_STORE,
            input_hash="a" * 64,
            output_hash="b" * 64,
        )

        manifest = EvidenceManifestBuilder.build(run)

        assert manifest.input_sha256 == "a" * 64
        assert manifest.output_envelope_sha256 == "b" * 64

    def test_retention_does_not_change_manifest_shape(self):
        """Verifiers see one stable receipt regardless of payload policy."""

        run = _completed_run(
            output_retention=OutputRetention.DO_NOT_STORE,
            input_hash="a" * 64,
            output_hash="b" * 64,
        )

        first = EvidenceManifestBuilder.serialise(
            EvidenceManifestBuilder.build(run),
        )
        run.output_retention_policy = OutputRetention.STORE_30_DAYS
        second = EvidenceManifestBuilder.serialise(
            EvidenceManifestBuilder.build(run),
        )

        assert first == second

    def test_permanent_receipt_has_no_retention_or_redaction_fields(self):
        """The schema itself, rather than runtime redaction, is the allowlist."""

        run = _completed_run(
            output_retention=OutputRetention.STORE_PERMANENTLY,
            input_hash="c" * 64,
            output_hash="d" * 64,
        )

        keys = set(
            EvidenceManifestBuilder.build(run).model_dump(
                mode="json",
                by_alias=True,
            ),
        )

        assert "retention" not in keys
        assert "redactions_applied" not in keys
