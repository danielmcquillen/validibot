"""Validator backend image cosign verification.

ADR-2026-04-27 Phase 5 Session A.2 — optional pre-launch verification
that a validator backend container image was signed by a key the
deployment trusts. When enabled via
:setting:`COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES`, the runner shells
out to the ``cosign`` CLI before launching each container and aborts
the run if signature verification fails. When disabled (the
default), this module is a structured no-op.

Why we shell out instead of reimplementing
==========================================

Sigstore's signature verification is a moving target. The protocol
involves transparency-log inclusion proofs, OIDC certificate
chains, and Rekor entry validation — all of which the upstream
``cosign`` CLI already gets right and updates regularly. Re-
implementing in Python would mean tracking sigstore protocol
changes ourselves, which is exactly the kind of crypto plumbing
work that ADR-2026-04-27 says we delegate to mature tools.

The shell-out strategy:

- We invoke ``cosign verify --key <key_path> <image_ref>`` with a
  short timeout.
- Exit code 0 means signature verified; non-zero means failed (or
  cosign couldn't reach the registry, etc.).
- A missing cosign binary, missing key file, or timeout is a
  *configuration* error and aborts the run with a clear message —
  the deployment opted into verification, so silently skipping
  would defeat the purpose.

When verification is disabled, this module's
:func:`verify_image_signature` returns immediately with a "not
attempted" result and the runner proceeds. That keeps the call
site clean: it always asks "is this image signed?" and gets a
structured answer; the answer carries enough context to log or
act on.

What this module does NOT do
============================

- Cloud Run Job verification: Cloud Run already integrates with
  GCP Binary Authorization, which provides equivalent guarantees
  via a different mechanism. The Cloud Run launch path leaves
  cosign verification to that infrastructure rather than
  duplicating the check at application level.
- Keyless / OIDC-bound verification: future enhancement. Today's
  signing-key model uses a key pair we control; keyless flows
  (``cosign verify --certificate-identity``) come with the
  full self-service backend registration story.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings

logger = logging.getLogger(__name__)


class CosignVerifyOutcome(StrEnum):
    """The discrete outcomes the runner needs to distinguish.

    The runner only needs to know "should I launch the container
    or not?" but the structured outcome lets logs explain *why*
    a launch was refused.
    """

    # Verification was not attempted because the deployment hasn't
    # opted in. The runner proceeds normally.
    SKIPPED = "skipped"
    # Verification ran and the image is signed by the configured
    # key. Runner proceeds.
    VERIFIED = "verified"
    # Verification ran and the image is not signed (or the
    # signature didn't match the configured key). Runner aborts.
    SIGNATURE_INVALID = "signature_invalid"
    # Verification couldn't run because cosign isn't installed,
    # the key file is missing, or the subprocess timed out.
    # Runner aborts because the deployment asked for verification
    # and we couldn't honour the request.
    CONFIGURATION_ERROR = "configuration_error"


@dataclass(frozen=True)
class CosignVerifyResult:
    """Structured result of a cosign verification attempt.

    Frozen because the result flows through the runner and into
    log records — once produced, it shouldn't be mutated by
    downstream code.
    """

    outcome: CosignVerifyOutcome
    image_ref: str
    # Free-form message suitable for logs and operator-facing
    # error responses. Non-empty when ``outcome`` is failure or
    # configuration error; empty for SKIPPED / VERIFIED.
    message: str = ""

    @property
    def should_proceed(self) -> bool:
        """True when the runner is allowed to launch the container."""
        return self.outcome in {
            CosignVerifyOutcome.SKIPPED,
            CosignVerifyOutcome.VERIFIED,
        }


# Default subprocess timeout for ``cosign verify`` calls. Cosign
# verification is normally sub-second when the registry is healthy;
# 30 seconds is a generous budget that catches network stalls
# without blocking the runner indefinitely.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 30

# Maximum length of cosign stderr captured into a result message.
# Verification failures tend to be short; certificate-chain dumps
# from sigstore can be kilobytes. Cap so log records stay bounded
# without losing the leading diagnostic.
MAX_CAPTURED_STDERR_CHARS = 500


def is_cosign_enabled() -> bool:
    """Return True when the deployment has opted into verification."""
    return bool(
        getattr(settings, "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES", False),
    )


def verify_image_signature(
    image_ref: str,
    *,
    key_path: str | None = None,
    cosign_binary: str | None = None,
    timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> CosignVerifyResult:
    """Verify ``image_ref`` was signed by the configured cosign key.

    When :func:`is_cosign_enabled` returns False, this function
    returns ``SKIPPED`` immediately without invoking cosign or
    inspecting the deployment's settings further.

    When enabled, it shells out to ``cosign verify --key <key>
    <image>`` with the configured key path. Exit code 0 means
    verified; any non-zero exit (or subprocess failure) means
    refused. The runner caller should consult
    :attr:`CosignVerifyResult.should_proceed` and abort the run
    when False.

    Arguments:
        image_ref: The container image reference to verify. Most
            useful with a digest-pinned reference
            (``registry/path@sha256:...``) — verifying a tag
            reference works but only confirms "the tag's *current*
            content is signed," which can change.
        key_path: Override the configured public key path. Mostly
            for tests; production should rely on the setting.
        cosign_binary: Override the configured cosign binary. Same
            reasoning.
        timeout_seconds: Subprocess timeout. Defaults to a generous
            30 seconds; tests can use shorter values.

    Returns:
        A :class:`CosignVerifyResult` with outcome + diagnostic
        message.
    """
    if not is_cosign_enabled():
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.SKIPPED,
            image_ref=image_ref,
        )

    resolved_key = key_path or getattr(settings, "COSIGN_VERIFY_PUBLIC_KEY_PATH", "")
    if not resolved_key:
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.CONFIGURATION_ERROR,
            image_ref=image_ref,
            message=(
                "Cosign verification is enabled but "
                "COSIGN_VERIFY_PUBLIC_KEY_PATH is not configured."
            ),
        )

    resolved_binary = (
        cosign_binary or getattr(settings, "COSIGN_BINARY_PATH", "cosign") or "cosign"
    )

    # Resolve the binary on PATH up front so a clear "cosign not
    # installed" message is possible (rather than the more cryptic
    # FileNotFoundError that subprocess.run would raise downstream).
    if not _binary_is_available(resolved_binary):
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.CONFIGURATION_ERROR,
            image_ref=image_ref,
            message=(
                f"Cosign binary not found at '{resolved_binary}'. Install "
                "cosign on the worker or set COSIGN_BINARY_PATH to a valid "
                "binary location."
            ),
        )

    cmd = [resolved_binary, "verify", "--key", resolved_key, image_ref]
    try:
        completed = subprocess.run(  # noqa: S603 - args are constructed deterministically
            cmd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.CONFIGURATION_ERROR,
            image_ref=image_ref,
            message=(
                f"Cosign verification timed out after {timeout_seconds}s. "
                "Check registry connectivity from the worker."
            ),
        )
    except FileNotFoundError:
        # Defensive — should be caught by _binary_is_available above,
        # but covers the race where the binary disappears between
        # the lookup and the call.
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.CONFIGURATION_ERROR,
            image_ref=image_ref,
            message=f"Cosign binary disappeared at '{resolved_binary}'.",
        )

    if completed.returncode == 0:
        return CosignVerifyResult(
            outcome=CosignVerifyOutcome.VERIFIED,
            image_ref=image_ref,
        )

    # Non-zero exit — verification failed. Cosign writes details to
    # stderr; surface a truncated form so the operator sees *why*
    # without flooding logs with a full chain trace.
    stderr_tail = (completed.stderr or "").strip()
    if len(stderr_tail) > MAX_CAPTURED_STDERR_CHARS:
        stderr_tail = stderr_tail[:MAX_CAPTURED_STDERR_CHARS] + "…"
    return CosignVerifyResult(
        outcome=CosignVerifyOutcome.SIGNATURE_INVALID,
        image_ref=image_ref,
        message=(
            f"Cosign verification failed (exit {completed.returncode}). "
            f"{stderr_tail}".strip()
        ),
    )


def _binary_is_available(binary: str) -> bool:
    """Return True when ``binary`` resolves on PATH or is a real file."""
    return shutil.which(binary) is not None
