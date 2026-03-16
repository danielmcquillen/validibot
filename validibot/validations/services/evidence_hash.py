"""
Evidence hash computation for validation runs.

Produces a deterministic SHA-256 hash over the canonical fields of a
completed validation run.  This hash serves as a tamper-evident seal:
if any of the covered fields change after completion, the hash will no
longer match, signaling that the record has been modified.

Covered fields (in canonical JSON order):
    - run_id (UUID)
    - content_hash (SHA-256 of submitted content)
    - user_id (integer, nullable)
    - submission_name (string, nullable)
    - status (e.g. "SUCCEEDED", "FAILED")
    - started_at (ISO 8601 UTC)
    - ended_at (ISO 8601 UTC)
    - duration_ms (integer)
    - error_count (integer)
    - warning_count (integer)
    - info_count (integer)
    - total_findings (integer)

The hash is computed once at run completion and stored on the
``ValidationRun.evidence_hash`` field.  It is also included in API
responses so external systems can verify record integrity.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


def compute_evidence_hash(run: ValidationRun) -> str:
    """Compute the SHA-256 evidence hash for a completed run.

    Builds a canonical JSON document from the run's immutable fields
    and returns its hex-encoded SHA-256 digest.  The JSON keys are
    sorted to ensure deterministic output regardless of Python dict
    ordering.

    Args:
        run: A ValidationRun that has reached a terminal status.

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    summary = getattr(run, "summary_record", None)

    canonical = {
        "run_id": str(run.id),
        "content_hash": (run.submission.checksum_sha256 if run.submission else ""),
        "user_id": run.user_id,
        "submission_name": (
            run.submission.name if run.submission and run.submission.name else None
        ),
        "status": run.status,
        "started_at": (run.started_at.isoformat() if run.started_at else None),
        "ended_at": (run.ended_at.isoformat() if run.ended_at else None),
        "duration_ms": run.duration_ms,
        "error_count": summary.error_count if summary else 0,
        "warning_count": summary.warning_count if summary else 0,
        "info_count": summary.info_count if summary else 0,
        "total_findings": summary.total_findings if summary else 0,
    }

    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stamp_evidence_hash(run: ValidationRun) -> str:
    """Compute and persist the evidence hash on a completed run.

    Computes the hash via ``compute_evidence_hash()``, saves it to
    ``run.evidence_hash``, and returns the digest string.

    This should be called exactly once, after the run summary record
    has been built (so finding counts are available).
    """
    digest = compute_evidence_hash(run)
    run.evidence_hash = digest
    run.save(update_fields=["evidence_hash"])
    logger.debug("Evidence hash stamped for run %s: %s", run.id, digest)
    return digest


def safe_stamp_evidence_hash(run: ValidationRun) -> None:
    """Stamp the evidence hash, logging but never raising on failure.

    The evidence hash is a tamper-evident seal — valuable for audit
    integrity but never worth blocking run finalization.  If hashing
    fails (e.g., null submission, DB error), we log the exception and
    continue so that the rest of the finalization (receipt updates,
    submission purge, task result) still completes.

    Use this wrapper at all finalization call sites instead of
    duplicating the try/except pattern around ``stamp_evidence_hash()``.
    """
    try:
        stamp_evidence_hash(run)
    except Exception:
        logger.exception(
            "Failed to stamp evidence hash for run %s — "
            "finalization will continue without it.",
            run.id,
        )
