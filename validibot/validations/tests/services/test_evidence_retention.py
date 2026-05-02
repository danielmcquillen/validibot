"""Tests for the evidence-manifest retention policy + Session B builder wiring.

ADR-2026-04-27 Phase 4 Session B (tasks 5-7 + remaining 11): the
manifest builder respects the workflow's retention class. Input
hashes always land in the manifest (preimage-resistant; safe under
DO_NOT_STORE); output hashes are gated.

What this file pins down
========================

1. ``RetentionPolicy`` is the single decision point — pure policy,
   no DB / file IO.
2. ``EvidenceManifestBuilder.build`` consults the policy and
   populates ``payload_digests`` accordingly.
3. ``DO_NOT_STORE`` runs produce a manifest with input hash present
   but output hash absent. The redaction is recorded in
   ``retention.redactions_applied``.
4. Non-``DO_NOT_STORE`` runs include both hashes.
5. The manifest's existing identity / contract / validator
   metadata is unchanged regardless of retention — the contract
   that proves "the run happened" survives every retention class.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.services.evidence_retention import PAYLOAD_DIGEST_OUTPUT
from validibot.validations.services.evidence_retention import RetentionPolicy
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _completed_run_with_hashes(
    *,
    data_retention: str,
    input_hash: str = "a" * 64,
    output_hash: str = "b" * 64,
):
    """Build a run with a submission, an output hash, and a chosen retention."""
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON],
        data_retention=data_retention,
    )
    WorkflowStepFactory(workflow=workflow)
    submission = SubmissionFactory(
        workflow=workflow,
        checksum_sha256=input_hash,
    )
    run = ValidationRunFactory(
        workflow=workflow,
        submission=submission,
        status=ValidationRunStatus.SUCCEEDED,
        ended_at=datetime(2026, 5, 2, tzinfo=UTC),
        output_hash=output_hash,
    )
    return run


# ──────────────────────────────────────────────────────────────────────
# RetentionPolicy — pure decisions
# ──────────────────────────────────────────────────────────────────────


class TestRetentionPolicy:
    """The policy is a pure dispatch table; no DB or IO involved."""

    def test_input_hash_always_included(self):
        """Input hash is preimage-resistant; safe under every retention class."""
        for tier in [
            SubmissionRetention.DO_NOT_STORE,
            SubmissionRetention.STORE_30_DAYS,
            SubmissionRetention.STORE_PERMANENTLY,
            "",  # missing / unset
        ]:
            assert RetentionPolicy.includes_input_hash(tier) is True

    def test_output_hash_omitted_for_do_not_store(self):
        """DO_NOT_STORE strips the output hash."""
        assert (
            RetentionPolicy.includes_output_hash(SubmissionRetention.DO_NOT_STORE)
            is False
        )

    def test_output_hash_included_for_store_tiers(self):
        """STORE_* retention tiers include the output hash."""
        for tier in [
            SubmissionRetention.STORE_1_DAY,
            SubmissionRetention.STORE_7_DAYS,
            SubmissionRetention.STORE_30_DAYS,
            SubmissionRetention.STORE_PERMANENTLY,
        ]:
            assert RetentionPolicy.includes_output_hash(tier) is True

    def test_redactions_for_do_not_store_lists_output(self):
        """DO_NOT_STORE redactions list mentions the omitted output digest field."""
        redactions = RetentionPolicy.redactions_for(
            SubmissionRetention.DO_NOT_STORE,
        )
        assert PAYLOAD_DIGEST_OUTPUT in redactions

    def test_redactions_empty_for_permissive_retention(self):
        """Permissive retention -> nothing was redacted, list is empty."""
        redactions = RetentionPolicy.redactions_for(
            SubmissionRetention.STORE_30_DAYS,
        )
        assert redactions == []


# ──────────────────────────────────────────────────────────────────────
# Builder respects retention policy
# ──────────────────────────────────────────────────────────────────────


class TestBuilderUnderDoNotStore:
    """A DO_NOT_STORE run produces a privacy-respecting manifest."""

    def test_do_not_store_run_includes_input_hash(self):
        """The input hash IS preserved through DO_NOT_STORE.

        That's the proof "this run consumed *this exact input*"
        that makes the manifest meaningful even after the bytes
        themselves are purged.
        """
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.DO_NOT_STORE,
            input_hash="c" * 64,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.payload_digests.input_sha256 == "c" * 64

    def test_do_not_store_run_omits_output_hash(self):
        """The output hash is dropped under DO_NOT_STORE."""
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.DO_NOT_STORE,
            output_hash="d" * 64,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.payload_digests.output_envelope_sha256 is None

    def test_do_not_store_run_records_redaction_in_retention(self):
        """The omission is recorded in retention.redactions_applied.

        Externally observable audit trail: a verifier reading the
        manifest can see "this manifest deliberately omits the
        output hash; the policy says so" rather than guessing
        whether the hash is missing because of policy or bug.
        """
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert PAYLOAD_DIGEST_OUTPUT in manifest.retention.redactions_applied

    def test_do_not_store_run_preserves_identity_and_contract(self):
        """The non-payload portion of the manifest is unchanged.

        DO_NOT_STORE strips payload, not the workflow identity. The
        run, workflow, validator metadata, and input schema all
        stay — that's the proof the run happened, ran under known
        rules, and consumed an input matching the recorded hash.
        """
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.run_id == str(run.id)
        assert manifest.workflow_slug == run.workflow.slug
        assert manifest.workflow_contract.data_retention == "DO_NOT_STORE"
        assert len(manifest.steps) == 1


class TestBuilderUnderPermissiveRetention:
    """Non-DO_NOT_STORE runs include the full payload-digest pair."""

    def test_store_30_days_includes_both_hashes(self):
        """Both input and output hashes land in the manifest."""
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.STORE_30_DAYS,
            input_hash="e" * 64,
            output_hash="f" * 64,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.payload_digests.input_sha256 == "e" * 64
        assert manifest.payload_digests.output_envelope_sha256 == "f" * 64

    def test_permissive_retention_has_empty_redactions_list(self):
        """Nothing was stripped -> redactions_applied stays empty."""
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.STORE_PERMANENTLY,
        )
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.retention.redactions_applied == []


# ──────────────────────────────────────────────────────────────────────
# Manifest hash stability across hash-bearing fields
# ──────────────────────────────────────────────────────────────────────


class TestManifestHashStabilityWithDigests:
    """Adding digests must not break hash determinism (Session A property)."""

    def test_serialised_manifest_with_digests_is_deterministic(self):
        """Same input -> same canonical bytes -> same hash, with digests populated."""
        run = _completed_run_with_hashes(
            data_retention=SubmissionRetention.STORE_30_DAYS,
            input_hash="1" * 64,
            output_hash="2" * 64,
        )
        manifest = EvidenceManifestBuilder.build(run)
        b1 = EvidenceManifestBuilder.serialise(manifest)
        b2 = EvidenceManifestBuilder.serialise(manifest)
        assert b1 == b2

    def test_redaction_changes_canonical_bytes(self):
        """Different retention class -> different manifest -> different bytes.

        Sanity check: the manifest is sensitive to retention. A
        DO_NOT_STORE run and an otherwise-identical STORE_30_DAYS
        run produce DIFFERENT manifests (and hashes) because the
        DO_NOT_STORE one omits the output digest + records the
        redaction.
        """
        run_dns = _completed_run_with_hashes(
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        run_30 = _completed_run_with_hashes(
            data_retention=SubmissionRetention.STORE_30_DAYS,
        )
        m_dns = EvidenceManifestBuilder.build(run_dns)
        m_30 = EvidenceManifestBuilder.build(run_30)
        bytes_dns = EvidenceManifestBuilder.serialise(m_dns)
        bytes_30 = EvidenceManifestBuilder.serialise(m_30)
        assert bytes_dns != bytes_30
