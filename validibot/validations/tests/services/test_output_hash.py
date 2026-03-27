"""
Tests for the output-hash service — tamper-evident seals on validation runs.

The output hash is a SHA-256 digest computed over a canonical JSON document
built from a completed validation run's immutable fields (run_id, content_hash,
user_id, status, timing, finding counts).  Once stamped, the hash serves as a
tamper-evident seal: any modification to the covered fields will cause a mismatch.

These tests verify:

1. **Determinism** — the same run always produces the same hash.
2. **Sensitivity** — changing any covered field produces a different hash.
3. **Null safety** — missing or null fields don't crash the computation.
4. **Persistence** — ``stamp_output_hash()`` writes the hash to the DB.
5. **Format** — the hash is a valid 64-character lowercase hex string.

See Also:
    - ``validibot.validations.services.output_hash`` — implementation
    - ``validibot.validations.models.ValidationRun.output_hash`` — field
"""

from __future__ import annotations

import contextlib

import pytest
from django.core.exceptions import ImproperlyConfigured

from validibot.validations.services.output_hash import compute_output_hash
from validibot.validations.services.output_hash import register_output_hash_provider
from validibot.validations.services.output_hash import reset_output_hash_provider
from validibot.validations.services.output_hash import stamp_output_hash
from validibot.validations.tests.factories import ValidationRunFactory

pytestmark = pytest.mark.django_db

# SHA-256 produces a 64-character lowercase hex string.
SHA256_HEX_LENGTH = 64


@pytest.fixture(autouse=True)
def reset_provider_registry():
    """Keep provider registration isolated across tests."""
    reset_output_hash_provider()
    yield
    reset_output_hash_provider()


# ── compute_output_hash() — determinism & format ──────────────────────
#
# The hash must be deterministic (same input → same output) and produce
# a valid 64-character hex string.  These tests pin the format contract.


class TestComputeOutputHash:
    """Tests for ``compute_output_hash()`` — the pure computation."""

    def test_returns_64_char_hex_string(self):
        """The output hash must be a 64-character lowercase hex string
        (SHA-256 digest).  This format is the external API contract —
        consumers rely on it for display and verification."""
        run = ValidationRunFactory()
        digest = compute_output_hash(run)

        assert isinstance(digest, str)
        assert len(digest) == SHA256_HEX_LENGTH
        # Must be valid hex
        int(digest, 16)

    def test_deterministic_for_same_run(self):
        """Calling ``compute_output_hash()`` twice on the same run
        must produce identical results.  Non-determinism would make
        the tamper-evident seal useless — every check would fail."""
        run = ValidationRunFactory()
        digest1 = compute_output_hash(run)
        digest2 = compute_output_hash(run)

        assert digest1 == digest2

    def test_different_status_produces_different_hash(self):
        """Changing the run's status must produce a different hash.
        This verifies that the status field is actually included in
        the canonical document."""
        run = ValidationRunFactory(status="SUCCEEDED")
        hash_succeeded = compute_output_hash(run)

        run.status = "FAILED"
        hash_failed = compute_output_hash(run)

        assert hash_succeeded != hash_failed

    def test_different_user_produces_different_hash(self):
        """Changing the run's user_id must produce a different hash.
        This verifies that the user_id field participates in the
        canonical document."""
        run = ValidationRunFactory()
        hash_original = compute_output_hash(run)

        run.user_id = 99999
        hash_different_user = compute_output_hash(run)

        assert hash_original != hash_different_user

    def test_handles_null_submission(self):
        """Runs with ``submission=None`` should not crash.  This can
        happen during error paths where the submission was never
        attached.  The hash should still be computable."""
        run = ValidationRunFactory()
        run.submission = None

        # Should not raise
        digest = compute_output_hash(run)
        assert len(digest) == SHA256_HEX_LENGTH

    def test_handles_null_timestamps(self):
        """Runs with null ``started_at`` or ``ended_at`` (e.g.,
        cancelled before starting) should produce a valid hash
        with ``None`` in the canonical document."""
        run = ValidationRunFactory()
        run.started_at = None
        run.ended_at = None

        digest = compute_output_hash(run)
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

        digest = compute_output_hash(run)
        assert len(digest) == SHA256_HEX_LENGTH

    def test_uses_registered_provider_when_available(self):
        """A registered provider should override the fallback contract."""

        run = ValidationRunFactory()

        def provider(_run):
            return "output-hash"

        register_output_hash_provider(
            provider,
            provider_name="tests.output_hash_provider",
        )

        digest = compute_output_hash(run)

        assert digest == "output-hash"

    def test_rejects_multiple_registered_providers(self):
        """Only one explicit provider may own the output-hash contract."""

        register_output_hash_provider(
            lambda _run: "first-hash",
            provider_name="tests.first_provider",
        )

        with pytest.raises(ImproperlyConfigured, match="Only one output-hash"):
            register_output_hash_provider(
                lambda _run: "second-hash",
                provider_name="tests.second_provider",
            )


# ── stamp_output_hash() — persistence ────────────────────────────────
#
# ``stamp_output_hash()`` computes the hash AND persists it to the
# database.  These tests verify the full write path.


class TestStampOutputHash:
    """Tests for ``stamp_output_hash()`` — compute + persist."""

    def test_persists_hash_to_database(self):
        """After stamping, ``run.output_hash`` should be populated
        both on the in-memory object and in the database.  This is
        the primary contract of ``stamp_output_hash()``."""
        run = ValidationRunFactory()
        assert not run.output_hash  # Starts empty

        digest = stamp_output_hash(run)

        # In-memory object updated
        assert run.output_hash == digest
        # Database row updated
        run.refresh_from_db()
        assert run.output_hash == digest
        assert len(run.output_hash) == SHA256_HEX_LENGTH

    def test_returns_the_computed_hash(self):
        """The return value of ``stamp_output_hash()`` must be the
        same digest that was persisted, so callers can use it without
        re-reading from the database."""
        run = ValidationRunFactory()
        returned = stamp_output_hash(run)

        assert returned == run.output_hash

    def test_hash_matches_recomputation(self):
        """The persisted hash must match a fresh computation.  This
        verifies that ``stamp_output_hash()`` doesn't somehow
        produce a different value than ``compute_output_hash()``."""
        run = ValidationRunFactory()
        stamp_output_hash(run)

        recomputed = compute_output_hash(run)
        assert run.output_hash == recomputed

    def test_idempotent_re_stamping(self):
        """Stamping the same run twice should produce the same hash
        (assuming no fields changed).  This is important for retry
        scenarios where the orchestrator might call stamp twice."""
        run = ValidationRunFactory()
        hash1 = stamp_output_hash(run)
        hash2 = stamp_output_hash(run)

        assert hash1 == hash2
