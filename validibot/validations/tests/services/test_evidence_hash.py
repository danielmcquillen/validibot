"""
Tests for the evidence hash service — tamper-evident seals on validation runs.

The evidence hash is a SHA-256 digest computed over a canonical JSON document
built from a completed validation run's immutable fields (run_id, content_hash,
user_id, status, timing, finding counts).  Once stamped, the hash serves as a
tamper-evident seal: any modification to the covered fields will cause a mismatch.

These tests verify:

1. **Determinism** — the same run always produces the same hash.
2. **Sensitivity** — changing any covered field produces a different hash.
3. **Null safety** — missing or null fields don't crash the computation.
4. **Persistence** — ``stamp_evidence_hash()`` writes the hash to the DB.
5. **Format** — the hash is a valid 64-character lowercase hex string.

See Also:
    - ``validibot.validations.services.evidence_hash`` — implementation
    - ``validibot.validations.models.ValidationRun.evidence_hash`` — field
"""

from __future__ import annotations

import contextlib

import pytest

from validibot.validations.services.evidence_hash import compute_evidence_hash
from validibot.validations.services.evidence_hash import stamp_evidence_hash
from validibot.validations.tests.factories import ValidationRunFactory

pytestmark = pytest.mark.django_db

# SHA-256 produces a 64-character lowercase hex string.
SHA256_HEX_LENGTH = 64


# ── compute_evidence_hash() — determinism & format ────────────────────
#
# The hash must be deterministic (same input → same output) and produce
# a valid 64-character hex string.  These tests pin the format contract.


class TestComputeEvidenceHash:
    """Tests for ``compute_evidence_hash()`` — the pure computation."""

    def test_returns_64_char_hex_string(self):
        """The evidence hash must be a 64-character lowercase hex string
        (SHA-256 digest).  This format is the external API contract —
        consumers rely on it for display and verification."""
        run = ValidationRunFactory()
        digest = compute_evidence_hash(run)

        assert isinstance(digest, str)
        assert len(digest) == SHA256_HEX_LENGTH
        # Must be valid hex
        int(digest, 16)

    def test_deterministic_for_same_run(self):
        """Calling ``compute_evidence_hash()`` twice on the same run
        must produce identical results.  Non-determinism would make
        the tamper-evident seal useless — every check would fail."""
        run = ValidationRunFactory()
        digest1 = compute_evidence_hash(run)
        digest2 = compute_evidence_hash(run)

        assert digest1 == digest2

    def test_different_status_produces_different_hash(self):
        """Changing the run's status must produce a different hash.
        This verifies that the status field is actually included in
        the canonical document."""
        run = ValidationRunFactory(status="SUCCEEDED")
        hash_succeeded = compute_evidence_hash(run)

        run.status = "FAILED"
        hash_failed = compute_evidence_hash(run)

        assert hash_succeeded != hash_failed

    def test_different_user_produces_different_hash(self):
        """Changing the run's user_id must produce a different hash.
        This verifies that the user_id field participates in the
        canonical document."""
        run = ValidationRunFactory()
        hash_original = compute_evidence_hash(run)

        run.user_id = 99999
        hash_different_user = compute_evidence_hash(run)

        assert hash_original != hash_different_user

    def test_handles_null_submission(self):
        """Runs with ``submission=None`` should not crash.  This can
        happen during error paths where the submission was never
        attached.  The hash should still be computable."""
        run = ValidationRunFactory()
        run.submission = None

        # Should not raise
        digest = compute_evidence_hash(run)
        assert len(digest) == SHA256_HEX_LENGTH

    def test_handles_null_timestamps(self):
        """Runs with null ``started_at`` or ``ended_at`` (e.g.,
        cancelled before starting) should produce a valid hash
        with ``None`` in the canonical document."""
        run = ValidationRunFactory()
        run.started_at = None
        run.ended_at = None

        digest = compute_evidence_hash(run)
        assert len(digest) == SHA256_HEX_LENGTH

    def test_handles_missing_summary_record(self):
        """Runs without a ``summary_record`` (e.g., crashed before
        summary building) should produce a valid hash with zero
        counts for all finding fields."""
        run = ValidationRunFactory()
        # Delete summary_record if it exists.  suppress() handles the case
        # where the factory didn't create one (RelatedObjectDoesNotExist).
        if hasattr(run, "summary_record"):
            with contextlib.suppress(Exception):
                run.summary_record.delete()
                run.refresh_from_db()

        digest = compute_evidence_hash(run)
        assert len(digest) == SHA256_HEX_LENGTH


# ── stamp_evidence_hash() — persistence ───────────────────────────────
#
# ``stamp_evidence_hash()`` computes the hash AND persists it to the
# database.  These tests verify the full write path.


class TestStampEvidenceHash:
    """Tests for ``stamp_evidence_hash()`` — compute + persist."""

    def test_persists_hash_to_database(self):
        """After stamping, ``run.evidence_hash`` should be populated
        both on the in-memory object and in the database.  This is
        the primary contract of ``stamp_evidence_hash()``."""
        run = ValidationRunFactory()
        assert not run.evidence_hash  # Starts empty

        digest = stamp_evidence_hash(run)

        # In-memory object updated
        assert run.evidence_hash == digest
        # Database row updated
        run.refresh_from_db()
        assert run.evidence_hash == digest
        assert len(run.evidence_hash) == SHA256_HEX_LENGTH

    def test_returns_the_computed_hash(self):
        """The return value of ``stamp_evidence_hash()`` must be the
        same digest that was persisted, so callers can use it without
        re-reading from the database."""
        run = ValidationRunFactory()
        returned = stamp_evidence_hash(run)

        assert returned == run.evidence_hash

    def test_hash_matches_recomputation(self):
        """The persisted hash must match a fresh computation.  This
        verifies that ``stamp_evidence_hash()`` doesn't somehow
        produce a different value than ``compute_evidence_hash()``."""
        run = ValidationRunFactory()
        stamp_evidence_hash(run)

        recomputed = compute_evidence_hash(run)
        assert run.evidence_hash == recomputed

    def test_idempotent_re_stamping(self):
        """Stamping the same run twice should produce the same hash
        (assuming no fields changed).  This is important for retry
        scenarios where the orchestrator might call stamp twice."""
        run = ValidationRunFactory()
        hash1 = stamp_evidence_hash(run)
        hash2 = stamp_evidence_hash(run)

        assert hash1 == hash2
