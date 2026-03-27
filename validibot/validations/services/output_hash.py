"""Output-hash computation and registration for validation runs.

This module owns the community-side Layer 3 output-hash contract.
Community Validibot always has a fallback hash shape, but installed
commercial packages may register one explicit provider that computes a
different canonical output hash.

Only one provider may be active at a time. That keeps the hash contract
deterministic and avoids package-order ambiguity at startup.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING
from typing import Protocol

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)
_OUTPUT_HASH_PROVIDER_STATE: dict[str, OutputHashProvider | str | None] = {
    "provider": None,
    "provider_name": "",
}


class OutputHashProvider(Protocol):
    """Callable contract for an installed output-hash provider."""

    def __call__(self, run: ValidationRun) -> str | None:
        """Return a hash digest for ``run`` or ``None`` to defer to fallback."""


def register_output_hash_provider(
    provider: OutputHashProvider,
    *,
    provider_name: str,
) -> None:
    """Register the single active output-hash provider.

    Args:
        provider: Callable that returns an output hash for a run.
        provider_name: Stable name used in logs and conflict messages.

    Raises:
        ImproperlyConfigured: If a different provider is already registered.
    """
    if (
        _OUTPUT_HASH_PROVIDER_STATE["provider"] is provider
        and provider_name == _OUTPUT_HASH_PROVIDER_STATE["provider_name"]
    ):
        return
    if _OUTPUT_HASH_PROVIDER_STATE["provider"] is not None:
        raise ImproperlyConfigured(
            "Only one output-hash provider may be registered. "
            "Already registered: "
            f"{_OUTPUT_HASH_PROVIDER_STATE['provider_name']}; "
            f"attempted: {provider_name}.",
        )
    _OUTPUT_HASH_PROVIDER_STATE["provider"] = provider
    _OUTPUT_HASH_PROVIDER_STATE["provider_name"] = provider_name


def get_output_hash_provider() -> OutputHashProvider | None:
    """Return the currently registered output-hash provider, if any."""

    return _OUTPUT_HASH_PROVIDER_STATE["provider"]  # type: ignore[return-value]


def reset_output_hash_provider() -> None:
    """Clear the registered provider.

    This helper is primarily for tests that need a clean registry state.
    """
    _OUTPUT_HASH_PROVIDER_STATE["provider"] = None
    _OUTPUT_HASH_PROVIDER_STATE["provider_name"] = ""


def compute_output_hash(run: ValidationRun) -> str:
    """Compute the Layer 3 output hash for a completed run.

    If an explicit provider has been registered, it gets the first chance
    to produce the digest. If no provider is registered, or if the
    provider returns ``None``, community fallback logic is used.

    Args:
        run: A ValidationRun that has reached a terminal status.

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    provider = get_output_hash_provider()
    if provider is not None:
        try:
            delegated = provider(run)
        except Exception:
            logger.exception(
                "Registered output-hash provider %s failed for run %s. "
                "Falling back to the community contract.",
                _OUTPUT_HASH_PROVIDER_STATE["provider_name"],
                run.id,
            )
        else:
            if delegated:
                return delegated

    return _compute_fallback_output_hash(run)


def _compute_fallback_output_hash(run: ValidationRun) -> str:
    """Compute the built-in community output hash."""

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


def stamp_output_hash(run: ValidationRun) -> str:
    """Compute and persist the output hash on a completed run.

    Computes the hash via ``compute_output_hash()``, saves it to
    ``run.output_hash``, and returns the digest string.

    This should be called exactly once, after the run summary record
    has been built (so finding counts are available).
    """
    digest = compute_output_hash(run)
    run.output_hash = digest
    run.save(update_fields=["output_hash"])
    logger.debug("Output hash stamped for run %s: %s", run.id, digest)
    return digest


def safe_stamp_output_hash(run: ValidationRun) -> None:
    """Stamp the output hash, logging but never raising on failure.

    The output hash is a tamper-evident seal — valuable for audit
    integrity but never worth blocking run finalization.  If hashing
    fails (e.g., null submission, DB error), we log the exception and
    continue so that the rest of the finalization (receipt updates,
    submission purge, task result) still completes.

    Use this wrapper at all finalization call sites instead of
    duplicating the try/except pattern around ``stamp_output_hash()``.
    """
    try:
        stamp_output_hash(run)
    except Exception:
        logger.exception(
            "Failed to stamp output hash for run %s — "
            "finalization will continue without it.",
            run.id,
        )
