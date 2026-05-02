"""Validator backend image-pinning policy enforcement.

ADR-2026-04-27 Phase 5 Session B — operators can ratchet how
strictly validator backend images must be pinned. Three rungs:

- ``tag`` (default for community quick-start): floating tags like
  ``:latest`` are permitted. The runner records whatever digest it
  observes (Session A) but doesn't refuse runs.
- ``digest``: the validator's backend image *reference* must be
  pinned by sha256 digest (``registry/path@sha256:...``). Tag-only
  references are rejected before the container starts. This is the
  recommended posture for production self-hosted deployments — it
  prevents an attacker who got into our registry from silently
  swapping a known-good image's tag.
- ``signed-digest``: pinned by digest AND cosign-verified. Combines
  the digest-pin guarantee with proof the image was signed by the
  configured key. Requires ``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True``;
  the policy check enforces that pairing.

Why this is enforcement at runtime, not configuration time
==========================================================

We could imagine a startup-time validation that walks every
``Validator.image_name`` and rejects misconfigured ones. The runtime
check is preferred because:

1. ``Validator`` rows can be edited via Django admin between
   restarts. Startup-only enforcement misses post-startup changes.
2. The configured image string is just one input — deployments
   that override via ``settings.VALIDATOR_IMAGES`` or
   ``VALIDATOR_IMAGE_TAG`` could change behaviour without a
   restart. The runtime check sees the *resolved* image reference
   each launch.
3. Symmetric with the cosign helper (Session A.2) — both run
   immediately before launch, both can refuse, both produce
   structured results the runner converts to a clear error.

Why this lives next to cosign
=============================

The runner calls policy enforcement *and then* cosign verification.
A ``signed-digest`` deployment first verifies the image is digest-
pinned (cheap string check), and only then does cosign do the
expensive registry round-trip. Failing fast on string format
saves time and provides a clearer error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings


class ValidatorBackendImagePolicy(StrEnum):
    """The three policy rungs an operator can choose from.

    String values are stable identifiers — they appear in
    ``settings.VALIDATOR_BACKEND_IMAGE_POLICY`` and (eventually)
    ``check_validibot --json`` output. Treat as a public contract.
    """

    # Permits floating tags. The community-friendly default.
    TAG = "tag"
    # Image reference must be pinned by sha256 digest.
    DIGEST = "digest"
    # Pinned by digest AND cosign-verified.
    SIGNED_DIGEST = "signed-digest"


# Image references containing this substring are considered
# digest-pinned. Container reference syntax allows other digest
# algorithms (sha512), but cosign and Docker registries only emit
# sha256 in practice; matching the marker substring keeps the check
# simple and aligned with what runners actually produce.
DIGEST_PIN_MARKER = "@sha256:"


def is_digest_pinned(image_ref: str) -> bool:
    """Return True when ``image_ref`` is pinned by a sha256 digest.

    Examples:
        - ``gcr.io/example/foo@sha256:abc...`` → True
        - ``foo@sha256:abc...`` (no registry) → True
        - ``foo:v1`` → False
        - ``foo:latest`` → False
        - ``foo`` (no tag) → False — implicit ``:latest``

    Empty / falsy ``image_ref`` returns False (defensive: an
    unconfigured validator can't satisfy a digest policy).
    """
    if not image_ref:
        return False
    return DIGEST_PIN_MARKER in image_ref


@dataclass(frozen=True)
class ImagePolicyResult:
    """Outcome of an image-policy check.

    Mirrors the shape of :class:`CosignVerifyResult` so the runner
    can treat both as ``should_proceed`` gates with explanatory
    messages.
    """

    allowed: bool
    image_ref: str
    policy: ValidatorBackendImagePolicy
    message: str = ""

    @property
    def should_proceed(self) -> bool:
        """True when the runner is allowed to launch the container."""
        return self.allowed


def get_current_policy() -> ValidatorBackendImagePolicy:
    """Read the configured policy, defaulting to ``TAG`` when unset.

    Any unrecognised value falls back to ``TAG`` (the loose default)
    rather than crashing — misconfigured deployments stay running
    rather than failing every launch. The doctor command flags
    unrecognised values separately.
    """
    raw = getattr(settings, "VALIDATOR_BACKEND_IMAGE_POLICY", "")
    raw = (raw or "").strip().lower()
    try:
        return ValidatorBackendImagePolicy(raw)
    except ValueError:
        return ValidatorBackendImagePolicy.TAG


def enforce_image_policy(image_ref: str) -> ImagePolicyResult:
    """Check ``image_ref`` against the configured deployment policy.

    Returns a :class:`ImagePolicyResult` with ``allowed=True`` when
    the launch may proceed, ``allowed=False`` with an operator-
    facing message otherwise. The runner caller checks
    :attr:`ImagePolicyResult.should_proceed` and aborts with the
    message when False.

    Policy semantics:

    - ``tag``: always allowed.
    - ``digest``: ``image_ref`` must contain ``@sha256:``.
    - ``signed-digest``: must be digest-pinned AND
      ``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES`` must be enabled.
      The actual signature check lives in the cosign helper —
      this policy just refuses to run when the prerequisite
      configuration isn't there.
    """
    policy = get_current_policy()

    if policy == ValidatorBackendImagePolicy.TAG:
        return ImagePolicyResult(
            allowed=True,
            image_ref=image_ref,
            policy=policy,
        )

    # DIGEST and SIGNED_DIGEST both require digest-pinning.
    if not is_digest_pinned(image_ref):
        return ImagePolicyResult(
            allowed=False,
            image_ref=image_ref,
            policy=policy,
            message=(
                f"Policy '{policy.value}' requires digest-pinned validator "
                f"backend images, but '{image_ref}' is referenced by tag. "
                "Pin the image with '@sha256:<hex>' (or change "
                "VALIDATOR_BACKEND_IMAGE_POLICY to 'tag' for development)."
            ),
        )

    # SIGNED_DIGEST additionally requires cosign verification opted in.
    if policy == ValidatorBackendImagePolicy.SIGNED_DIGEST:
        cosign_enabled = bool(
            getattr(settings, "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES", False),
        )
        if not cosign_enabled:
            return ImagePolicyResult(
                allowed=False,
                image_ref=image_ref,
                policy=policy,
                message=(
                    "Policy 'signed-digest' requires "
                    "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True so the "
                    "runner verifies image signatures, but cosign "
                    "verification is disabled. Enable cosign or relax the "
                    "policy to 'digest'."
                ),
            )

    # Digest-pinned, and (if signed-digest) cosign is opted in.
    # The actual cosign verification still runs separately — this
    # check only ensures the prerequisite configuration is present.
    return ImagePolicyResult(
        allowed=True,
        image_ref=image_ref,
        policy=policy,
    )
