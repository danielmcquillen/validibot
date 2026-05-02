"""Tests for validator backend image cosign verification.

ADR-2026-04-27 Phase 5 Session A.2 — when
``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True`` is set, the runner
must shell out to the cosign CLI before launching each validator
backend container and abort the run if the image isn't signed by
the configured key.

This suite pins down four properties:

1. **Disabled is no-op.** With the setting off (the default for
   community deployments), :func:`verify_image_signature` returns
   ``SKIPPED`` without invoking subprocess at all. The runner
   proceeds normally.
2. **Verified passes through.** When cosign exits 0, the helper
   returns ``VERIFIED`` and ``should_proceed`` is True.
3. **Invalid signature aborts.** When cosign exits non-zero, the
   helper returns ``SIGNATURE_INVALID`` with the cosign stderr
   captured for the operator. ``should_proceed`` is False.
4. **Configuration errors abort.** Missing key path, missing
   binary, or subprocess timeout all yield ``CONFIGURATION_ERROR``
   — the deployment opted into verification, so silently skipping
   would defeat the security posture.

What's deliberately not covered here
====================================

- Real cosign invocations against a real registry: that belongs
  in a manual smoke test with signed images. Unit tests mock the
  subprocess so they're hermetic and fast.
- The Docker runner integration test: a separate harness asserts
  the runner's call site delegates to this helper correctly, but
  the helper's contract is what this file tests.
- Cloud Run integration: that path delegates to GCP Binary
  Authorization rather than this helper (see
  :mod:`validibot.validations.services.cosign` module docstring).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from django.test import override_settings

from validibot.validations.services.cosign import MAX_CAPTURED_STDERR_CHARS
from validibot.validations.services.cosign import CosignVerifyOutcome
from validibot.validations.services.cosign import is_cosign_enabled
from validibot.validations.services.cosign import verify_image_signature

# Upper bound for the truncation test: the helper caps captured
# stderr at MAX_CAPTURED_STDERR_CHARS and adds an ellipsis. We add
# a safety margin so the test doesn't break on an off-by-a-few char
# message prefix from the helper itself.
TRUNCATED_STDERR_UPPER_BOUND = MAX_CAPTURED_STDERR_CHARS + 200

# ── Disabled = no-op ────────────────────────────────────────────────────


class TestCosignDisabled:
    """When the setting is False, no subprocess runs and the runner proceeds."""

    @override_settings(COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False)
    def test_is_cosign_enabled_returns_false(self):
        """The helper read of the setting matches what the runner sees."""
        assert is_cosign_enabled() is False

    @override_settings(COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False)
    def test_verify_returns_skipped_without_subprocess_call(self):
        """Disabled mode never invokes cosign — even mocking subprocess proves it.

        This is the community-default path: every advanced-validator
        run hits this code, so it must be a structural no-op rather
        than a subprocess.run call that happens to short-circuit.
        """
        with patch("validibot.validations.services.cosign.subprocess.run") as mock_run:
            result = verify_image_signature("registry/example@sha256:" + "a" * 64)
            assert result.outcome == CosignVerifyOutcome.SKIPPED
            assert result.should_proceed is True
            assert mock_run.call_count == 0


# ── Enabled + cosign verified ───────────────────────────────────────────


class TestCosignVerifiedPath:
    """When cosign exits 0, the helper returns VERIFIED."""

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="/keys/cosign.pub",
        COSIGN_BINARY_PATH="cosign",
    )
    def test_zero_exit_yields_verified_outcome(self):
        """Exit code 0 from cosign verify → VERIFIED + should_proceed True."""
        with (
            patch(
                "validibot.validations.services.cosign._binary_is_available"
            ) as mock_avail,
            patch("validibot.validations.services.cosign.subprocess.run") as mock_run,
        ):
            mock_avail.return_value = True
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            result = verify_image_signature(
                "registry/example@sha256:" + "a" * 64,
            )
            assert result.outcome == CosignVerifyOutcome.VERIFIED
            assert result.should_proceed is True
            assert result.message == ""
            # Verify the actual command shape so a refactor doesn't
            # silently change which key flag we pass.
            args = mock_run.call_args[0][0]
            assert args[0] == "cosign"
            assert "verify" in args
            assert "--key" in args
            assert "/keys/cosign.pub" in args


# ── Enabled + cosign rejected ───────────────────────────────────────────


class TestCosignRejectedPath:
    """When cosign exits non-zero, the helper returns SIGNATURE_INVALID."""

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="/keys/cosign.pub",
        COSIGN_BINARY_PATH="cosign",
    )
    def test_nonzero_exit_yields_invalid_signature_outcome(self):
        """Cosign rejection blocks the runner.

        The runner caller checks ``should_proceed`` and aborts the
        run. The captured stderr surfaces in the message so an
        operator reading logs sees *why* cosign refused.
        """
        with (
            patch(
                "validibot.validations.services.cosign._binary_is_available"
            ) as mock_avail,
            patch("validibot.validations.services.cosign.subprocess.run") as mock_run,
        ):
            mock_avail.return_value = True
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: no matching signatures found for key",
            )
            result = verify_image_signature(
                "registry/example@sha256:" + "a" * 64,
            )
            assert result.outcome == CosignVerifyOutcome.SIGNATURE_INVALID
            assert result.should_proceed is False
            assert "no matching signatures" in result.message
            assert "exit 1" in result.message

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="/keys/cosign.pub",
        COSIGN_BINARY_PATH="cosign",
    )
    def test_long_stderr_is_truncated(self):
        """Cosign's chain trace can be long; we cap the captured tail.

        Defensive: a misbehaving cosign run could fill the message
        with kilobytes of stderr. The helper truncates to keep log
        records bounded without losing the leading diagnostic.
        """
        long_err = "x" * 1000
        with (
            patch(
                "validibot.validations.services.cosign._binary_is_available"
            ) as mock_avail,
            patch("validibot.validations.services.cosign.subprocess.run") as mock_run,
        ):
            mock_avail.return_value = True
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=2,
                stdout="",
                stderr=long_err,
            )
            result = verify_image_signature("registry/example:tag")
            assert result.outcome == CosignVerifyOutcome.SIGNATURE_INVALID
            # Truncated to MAX_CAPTURED_STDERR_CHARS + ellipsis
            assert len(result.message) < TRUNCATED_STDERR_UPPER_BOUND
            assert result.message.endswith("…")


# ── Enabled + configuration broken ──────────────────────────────────────


class TestCosignConfigurationErrors:
    """Misconfiguration must abort the run with a clear diagnostic.

    The deployment opted into verification by setting
    ``COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True``. If we silently
    skipped on missing key or missing binary, we'd defeat the
    security posture the operator asked for. So every config
    failure mode is treated as an abort.
    """

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="",
    )
    def test_missing_key_path_returns_configuration_error(self):
        """No key configured → can't verify → abort.

        The message must direct the operator at the specific
        setting they need to configure, not just say "key missing."
        """
        result = verify_image_signature("registry/example:tag")
        assert result.outcome == CosignVerifyOutcome.CONFIGURATION_ERROR
        assert result.should_proceed is False
        assert "COSIGN_VERIFY_PUBLIC_KEY_PATH" in result.message

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="/keys/cosign.pub",
        COSIGN_BINARY_PATH="cosign",
    )
    def test_missing_cosign_binary_returns_configuration_error(self):
        """Cosign not installed → can't verify → abort with install hint.

        The message tells the operator they need to install cosign
        or set ``COSIGN_BINARY_PATH``, both of which are real fixes.
        """
        with patch(
            "validibot.validations.services.cosign._binary_is_available",
        ) as mock_avail:
            mock_avail.return_value = False
            result = verify_image_signature("registry/example:tag")
            assert result.outcome == CosignVerifyOutcome.CONFIGURATION_ERROR
            assert result.should_proceed is False
            assert "Install cosign" in result.message

    @override_settings(
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True,
        COSIGN_VERIFY_PUBLIC_KEY_PATH="/keys/cosign.pub",
        COSIGN_BINARY_PATH="cosign",
    )
    def test_subprocess_timeout_returns_configuration_error(self):
        """Cosign hanging on registry connection → time-out → abort.

        Connectivity issues to the signature registry can hang the
        verify call. We bound it with a timeout and treat expiration
        as a configuration / environment error so the run doesn't
        hang either.
        """
        with (
            patch(
                "validibot.validations.services.cosign._binary_is_available"
            ) as mock_avail,
            patch("validibot.validations.services.cosign.subprocess.run") as mock_run,
        ):
            mock_avail.return_value = True
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="cosign verify",
                timeout=30,
            )
            result = verify_image_signature("registry/example:tag")
            assert result.outcome == CosignVerifyOutcome.CONFIGURATION_ERROR
            assert result.should_proceed is False
            assert "timed out" in result.message


# ── CosignVerifyResult.should_proceed contract ──────────────────────────


class TestShouldProceedContract:
    """Only SKIPPED and VERIFIED let the runner proceed."""

    def test_skipped_proceeds(self):
        """Disabled deployments must not block any run."""
        from validibot.validations.services.cosign import CosignVerifyResult

        result = CosignVerifyResult(
            outcome=CosignVerifyOutcome.SKIPPED,
            image_ref="x",
        )
        assert result.should_proceed is True

    def test_verified_proceeds(self):
        """A successful verify is the happy path."""
        from validibot.validations.services.cosign import CosignVerifyResult

        result = CosignVerifyResult(
            outcome=CosignVerifyOutcome.VERIFIED,
            image_ref="x",
        )
        assert result.should_proceed is True

    @pytest.mark.parametrize(
        "blocking_outcome",
        [
            CosignVerifyOutcome.SIGNATURE_INVALID,
            CosignVerifyOutcome.CONFIGURATION_ERROR,
        ],
    )
    def test_failures_block(self, blocking_outcome):
        """Both failure modes block the runner — no silent passes."""
        from validibot.validations.services.cosign import CosignVerifyResult

        result = CosignVerifyResult(
            outcome=blocking_outcome,
            image_ref="x",
        )
        assert result.should_proceed is False
