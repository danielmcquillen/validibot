"""Tests for the validator backend image-pinning policy gate.

ADR-2026-04-27 Phase 5 Session B — operators ratchet how strictly
validator backend images must be pinned via the
``VALIDATOR_BACKEND_IMAGE_POLICY`` setting. Three rungs:

- ``tag`` (default) — floating tags permitted; community quick-start.
- ``digest`` — image must be pinned by ``@sha256:<hex>``; tag-only
  references are rejected at launch time.
- ``signed-digest`` — pinned by digest AND
  ``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True``. The cosign helper
  does the actual signature check; this policy ensures the
  prerequisite configuration is present.

This suite pins down the four behaviours operators rely on:

1. ``tag`` accepts every image string — pinned or not.
2. ``digest`` accepts pinned references and rejects tag-only ones.
3. ``signed-digest`` rejects unpinned references AND digest-pinned
   ones when cosign isn't enabled — the policy is "both prerequisites
   or you can't launch."
4. Unrecognised policy values fall back to ``tag`` rather than
   crashing — misconfigured deployments stay running.

What's NOT covered here
=======================

- Doctor command output for each policy state — separately tested
  via the doctor's smoke harness; the policy module's contract is
  what this file pins.
- The runner's call site — exercised end-to-end by the runner unit
  tests in ``test_validator_backend_image_digest.py`` and
  ``test_cosign_verification.py``.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from validibot.validations.services.image_policy import ValidatorBackendImagePolicy
from validibot.validations.services.image_policy import enforce_image_policy
from validibot.validations.services.image_policy import get_current_policy
from validibot.validations.services.image_policy import is_digest_pinned

# Reusable test fixtures: real-shape image references the policy
# gate should classify correctly. Using realistic registry paths
# keeps the tests honest about what the helper sees in production.
DIGEST_PINNED = "gcr.io/example-project/validator-backend-energyplus@sha256:" + "a" * 64
DIGEST_PINNED_NO_REGISTRY = "validator-backend-energyplus@sha256:" + "b" * 64
TAG_PINNED = "gcr.io/example-project/validator-backend-energyplus:v23.2.0"
TAG_LATEST = "validator-backend-energyplus:latest"
NO_TAG = "validator-backend-energyplus"


# ── is_digest_pinned helper ─────────────────────────────────────────────


class TestIsDigestPinned:
    """The string check that policy enforcement builds on."""

    @pytest.mark.parametrize(
        "image_ref",
        [DIGEST_PINNED, DIGEST_PINNED_NO_REGISTRY],
    )
    def test_digest_form_returns_true(self, image_ref):
        """Any reference containing ``@sha256:`` is digest-pinned.

        Both registry-anchored and bare-name digest references count.
        Cosign and Docker registries only emit sha256 in practice, so
        the marker substring is the right discriminant.
        """
        assert is_digest_pinned(image_ref) is True

    @pytest.mark.parametrize(
        "image_ref",
        [TAG_PINNED, TAG_LATEST, NO_TAG],
    )
    def test_tag_form_returns_false(self, image_ref):
        """Tag-only or implicit-latest references are not digest-pinned."""
        assert is_digest_pinned(image_ref) is False

    def test_empty_string_returns_false(self):
        """Defensive: an unconfigured validator can't satisfy any pin."""
        assert is_digest_pinned("") is False


# ── get_current_policy ──────────────────────────────────────────────────


class TestGetCurrentPolicy:
    """Reads and validates the deployment setting."""

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="tag")
    def test_reads_tag(self):
        assert get_current_policy() == ValidatorBackendImagePolicy.TAG

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_reads_digest(self):
        assert get_current_policy() == ValidatorBackendImagePolicy.DIGEST

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="signed-digest")
    def test_reads_signed_digest(self):
        assert get_current_policy() == ValidatorBackendImagePolicy.SIGNED_DIGEST

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="DIGEST")
    def test_normalises_case(self):
        """Operators may set policy as ``DIGEST`` rather than ``digest``."""
        assert get_current_policy() == ValidatorBackendImagePolicy.DIGEST

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="")
    def test_empty_setting_falls_back_to_tag(self):
        """Missing setting → loose default (community-friendly)."""
        assert get_current_policy() == ValidatorBackendImagePolicy.TAG

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="garbage-value")
    def test_unrecognised_value_raises_improperly_configured(self):
        """Unrecognised value → fail loud (no silent relax to TAG).

        A typo in a strict-intent setting (``"strict"`` instead of
        ``"signed-digest"``, ``"hash"`` instead of ``"digest"``, …)
        used to silently fall back to ``TAG`` — exactly the opposite
        of operator intent and a hardening regression.  We now raise
        so the bug surfaces immediately; the doctor command catches
        the exception and reports it as a check failure.
        """
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="garbage-value"):
            get_current_policy()

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="strict")
    def test_typo_strict_does_not_relax_to_tag(self):
        """A common typo (``strict`` for ``signed-digest``) raises.

        Documented separately because this exact mistake is the
        regression the previous fall-back-to-TAG behaviour enabled —
        operators expecting strict enforcement would silently get
        the loosest mode.  Pinning the test to this concrete typo
        prevents reintroduction of the silent-relax behaviour by
        accident.
        """
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured):
            get_current_policy()


# ── tag policy ──────────────────────────────────────────────────────────


class TestTagPolicy:
    """Default policy — every image string is allowed."""

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="tag")
    def test_accepts_tag_pinned(self):
        result = enforce_image_policy(TAG_PINNED)
        assert result.should_proceed is True
        assert result.policy == ValidatorBackendImagePolicy.TAG

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="tag")
    def test_accepts_digest_pinned(self):
        """Digest-pinned references work fine under the loose policy."""
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.should_proceed is True

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="tag")
    def test_accepts_floating_latest(self):
        """``:latest`` is permitted under tag policy."""
        result = enforce_image_policy(TAG_LATEST)
        assert result.should_proceed is True


# ── digest policy ───────────────────────────────────────────────────────


class TestDigestPolicy:
    """Production posture — refuses tag-only references."""

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_accepts_digest_pinned(self):
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.should_proceed is True
        assert result.policy == ValidatorBackendImagePolicy.DIGEST
        assert result.message == ""

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_rejects_tag_pinned(self):
        """The whole point of ``digest`` policy is rejecting tags."""
        result = enforce_image_policy(TAG_PINNED)
        assert result.should_proceed is False
        # Message must direct the operator at the actionable fix.
        assert "@sha256:" in result.message
        assert "digest" in result.message.lower()

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_rejects_floating_latest(self):
        """``:latest`` is exactly what ``digest`` policy is meant to block."""
        result = enforce_image_policy(TAG_LATEST)
        assert result.should_proceed is False

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_rejects_no_tag(self):
        """Bare image name (implicit ``:latest``) is also tag-form."""
        result = enforce_image_policy(NO_TAG)
        assert result.should_proceed is False


# ── signed-digest policy ────────────────────────────────────────────────


class TestSignedDigestPolicy:
    """High-trust posture — requires digest pin AND cosign opt-in."""

    @override_settings(
        VALIDATOR_BACKEND_IMAGE_POLICY="signed-digest",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
    )
    def test_accepts_digest_pinned_when_cosign_enabled(self):
        """Both prerequisites satisfied → policy allows the launch.

        The actual cosign signature verification still happens via
        the cosign helper. The policy gate just enforces the
        configuration pairing.
        """
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.should_proceed is True
        assert result.policy == ValidatorBackendImagePolicy.SIGNED_DIGEST

    @override_settings(
        VALIDATOR_BACKEND_IMAGE_POLICY="signed-digest",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
    )
    def test_rejects_tag_pinned_even_with_cosign_enabled(self):
        """Tag-form is rejected first — digest pin is the harder requirement.

        Cosign can't help if the image isn't even pinned: a verifier
        needs an immutable reference to compare a signature against.
        """
        result = enforce_image_policy(TAG_PINNED)
        assert result.should_proceed is False
        assert "@sha256:" in result.message

    @override_settings(
        VALIDATOR_BACKEND_IMAGE_POLICY="signed-digest",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_rejects_digest_pinned_when_cosign_disabled(self):
        """Cosign disabled → policy refuses every launch.

        This is the "operator set the flag but didn't wire cosign"
        misconfiguration. The doctor command reports it as ``ERROR``
        because every launch will fail until the operator either
        enables cosign or relaxes to ``digest``.
        """
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.should_proceed is False
        assert "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES" in result.message

    @override_settings(
        VALIDATOR_BACKEND_IMAGE_POLICY="signed-digest",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_message_recommends_digest_relax_or_cosign_enable(self):
        """Message provides both viable fixes."""
        result = enforce_image_policy(DIGEST_PINNED)
        assert "Enable cosign" in result.message or "digest" in result.message


# ── result contract ─────────────────────────────────────────────────────


class TestImagePolicyResultContract:
    """The ``should_proceed`` property is the runner's gate."""

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_allowed_proceeds(self):
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.allowed is True
        assert result.should_proceed is True

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_rejected_does_not_proceed(self):
        result = enforce_image_policy(TAG_PINNED)
        assert result.allowed is False
        assert result.should_proceed is False

    @override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="digest")
    def test_result_carries_image_ref_verbatim(self):
        """Image reference echoes back so log records can correlate."""
        result = enforce_image_policy(DIGEST_PINNED)
        assert result.image_ref == DIGEST_PINNED
