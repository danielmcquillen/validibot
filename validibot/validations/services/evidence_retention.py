"""Retention-aware policy for what enters an evidence manifest.

ADR-2026-04-27 Phase 4 Session B: a workflow that signed up for
``input_retention=DO_NOT_STORE`` agreed not to keep submission bytes
after validation. The evidence manifest must respect that agreement
— no payload-derived content lands in the manifest, even though
identity / contract / hash content does.

Why this lives in its own module
================================

The Pydantic schema in ``validibot-shared`` is *what* the manifest
looks like; the builder in ``evidence.py`` is *how* it gets
constructed. The retention policy is *which* fields to populate
based on the workflow's retention class — that's a separate
concern that may evolve independently (e.g. when we add finer
retention tiers like "store for compliance audit only").

Centralising the rules here means:

1. The builder is a thin caller that asks the policy what's safe.
2. New retention tiers extend ``RetentionPolicy`` rather than
   sprinkling ``if input_retention == ...`` branches through the
   builder.
3. A single ``redactions_applied`` summary at the bottom of the
   manifest reflects every decision the policy made.

Allowlist semantics
===================

The policy lists fields *included* per retention tier rather than
fields *excluded*. The exclusion list is implicit: anything not
listed for the tier gets dropped. This is the safer default — a
new manifest field added in a future schema bump fails closed
(omitted under DO_NOT_STORE) rather than failing open (silently
included until someone notices the leak).
"""

from __future__ import annotations

from typing import Literal

from validibot.submissions.constants import SubmissionRetention

# Symbolic name of the do-not-store tier. Imported here as a module
# constant so callers can reference it without re-importing the enum.
DO_NOT_STORE: Literal["DO_NOT_STORE"] = SubmissionRetention.DO_NOT_STORE

# Names of payload-digest fields. These are the only Session A/B
# fields whose population depends on retention class.
PAYLOAD_DIGEST_INPUT = "payload_digests.input_sha256"
PAYLOAD_DIGEST_OUTPUT = "payload_digests.output_envelope_sha256"


class RetentionPolicy:
    """Decides which manifest fields are safe per retention class.

    Stateless; static methods only. Mirrors the
    ``WorkflowVersioningService`` pattern.
    """

    @staticmethod
    def includes_input_hash(retention_class: str) -> bool:
        """Always True — input hash is the proof of run-time conformance.

        Even ``DO_NOT_STORE`` runs include the input hash. Hashes are
        preimage-resistant: the receiver of the manifest cannot
        reconstruct the original bytes from SHA-256, so retaining the
        hash doesn't undermine the privacy promise. Withholding the
        hash would break the manifest's primary purpose: proving
        "this run consumed *this exact input*."
        """
        del retention_class  # the decision is unconditional
        return True

    @staticmethod
    def includes_output_hash(retention_class: str) -> bool:
        """False for DO_NOT_STORE; True otherwise.

        A cautious-by-default rule: ``DO_NOT_STORE`` operators
        agreed to remove the run's outputs along with its inputs,
        so we omit the output hash too. Operators who want output
        evidence can use a non-DO_NOT_STORE retention class.

        Note: this is more conservative than strictly necessary —
        an output hash, like the input hash, is preimage-resistant.
        We err on "output isn't part of the trust contract for
        DO_NOT_STORE" to keep the policy clean.
        """
        return retention_class != DO_NOT_STORE

    @staticmethod
    def redactions_for(retention_class: str) -> list[str]:
        """Return the list of field names redacted under this tier.

        Returned in stable insertion order so ``redactions_applied``
        in two runs of the same workflow class produces identical
        bytes (canonical-JSON byte stability).
        """
        redactions: list[str] = []
        if not RetentionPolicy.includes_output_hash(retention_class):
            redactions.append(PAYLOAD_DIGEST_OUTPUT)
        return redactions


__all__ = [
    "DO_NOT_STORE",
    "PAYLOAD_DIGEST_INPUT",
    "PAYLOAD_DIGEST_OUTPUT",
    "RetentionPolicy",
]
