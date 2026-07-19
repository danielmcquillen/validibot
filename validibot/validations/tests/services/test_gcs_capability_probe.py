"""Tests for live provider proof of attempt-scoped GCS authority.

The rollout flag may claim attempt isolation only after Google Cloud enforces
the intended boundary. These tests pin the probe itself: it must use explicit
downscoped credentials, exercise every allowed and forbidden operation, clean
only its unique prefix with generation preconditions, emit no token, and fail
when a cross-attempt operation unexpectedly succeeds.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from django.test import override_settings
from google.api_core.exceptions import Forbidden

from validibot.validations.services.cloud_run import gcs_capability_probe
from validibot.validations.services.cloud_run.gcs_capability_probe import (
    GCSCapabilityProbeCheck,
)
from validibot.validations.services.cloud_run.gcs_capability_probe import (
    GCSCapabilityProbeReport,
)
from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    AttemptGCSRuntimeCapability,
)


def _provider_mocks(*, allow_cross_attempt_read: bool = False):
    """Build trusted/capability clients with deterministic provider behavior."""
    trusted_client = MagicMock(name="trusted_client")
    trusted_bucket = trusted_client.bucket.return_value
    allowed_control = MagicMock(name="allowed_control", generation="11")
    denied_control = MagicMock(name="denied_control", generation="12")

    def _trusted_blob(name):
        """Resolve the two setup identities under the random probe root."""
        if name.endswith("attempts/allowed/input.txt"):
            return allowed_control
        if name.endswith("attempts/denied/input.txt"):
            return denied_control
        raise AssertionError(f"Unexpected trusted blob: {name}")

    trusted_bucket.blob.side_effect = _trusted_blob
    cleanup_blobs = [
        MagicMock(name="cleanup_allowed", generation="21"),
        MagicMock(name="cleanup_denied", generation="22"),
    ]
    trusted_bucket.list_blobs.return_value = cleanup_blobs

    capability_client = MagicMock(name="capability_client")
    capability_bucket = capability_client.bucket.return_value
    allowed_input = MagicMock(name="capability_allowed_input")
    allowed_input.download_as_bytes.return_value = gcs_capability_probe._CONTROL_BYTES
    allowed_input.upload_from_string.side_effect = Forbidden("overwrite denied")
    allowed_input.delete.side_effect = Forbidden("delete denied")
    allowed_output = MagicMock(name="capability_allowed_output")
    denied_input = MagicMock(name="capability_denied_input")
    if allow_cross_attempt_read:
        denied_input.download_as_bytes.return_value = b"unexpected disclosure"
    else:
        denied_input.download_as_bytes.side_effect = Forbidden("read denied")
    denied_output = MagicMock(name="capability_denied_output")
    denied_output.upload_from_string.side_effect = Forbidden("create denied")

    def _capability_blob(name, **kwargs):
        """Resolve each operation target while accepting generation pinning."""
        del kwargs
        if name.endswith("attempts/allowed/input.txt"):
            return allowed_input
        if name.endswith("attempts/allowed/output.txt"):
            return allowed_output
        if name.endswith("attempts/denied/input.txt"):
            return denied_input
        if name.endswith("attempts/denied/output.txt"):
            return denied_output
        raise AssertionError(f"Unexpected capability blob: {name}")

    capability_bucket.blob.side_effect = _capability_blob
    return {
        "trusted_client": trusted_client,
        "trusted_bucket": trusted_bucket,
        "capability_client": capability_client,
        "cleanup_blobs": cleanup_blobs,
    }


def _issued_capability() -> AttemptGCSRuntimeCapability:
    """Return a secret-safe test capability with ample remaining lifetime."""
    return AttemptGCSRuntimeCapability(
        access_token="probe-secret-token",  # noqa: S106 - test fixture
        expires_at=datetime.now(UTC) + timedelta(minutes=50),
        allowed_prefix=(
            "gs://validation/runs/validibot-capability-probes/probe/attempts/allowed/"
        ),
        project_id="validibot-project",
        refresh_url="https://invalid.example/validibot-capability-probe",
    )


@patch.object(gcs_capability_probe, "issue_attempt_gcs_runtime_capability")
@patch.object(gcs_capability_probe.storage, "Client")
def test_probe_proves_allowed_and_forbidden_provider_operations(client, issue):
    """A healthy provider boundary passes six operations and trusted cleanup."""
    mocks = _provider_mocks()
    client.side_effect = [mocks["trusted_client"], mocks["capability_client"]]
    issue.return_value = _issued_capability()

    report = gcs_capability_probe.probe_attempt_gcs_runtime_capability(
        bucket_name="validation",
        project_id="validibot-project",
    )

    assert report.passed is True
    assert [check.name for check in report.checks] == [
        "allowed_generation_read",
        "allowed_create",
        "cross_attempt_read",
        "cross_attempt_create",
        "existing_object_overwrite",
        "object_delete",
        "trusted_cleanup",
    ]
    assert "probe-secret-token" not in repr(report)
    assert "probe-secret-token" not in json.dumps(report.as_dict())
    for cleanup_blob in mocks["cleanup_blobs"]:
        cleanup_blob.delete.assert_called_once_with(
            if_generation_match=int(cleanup_blob.generation)
        )


@patch.object(gcs_capability_probe, "issue_attempt_gcs_runtime_capability")
@patch.object(gcs_capability_probe.storage, "Client")
def test_probe_fails_when_cross_attempt_read_is_unexpectedly_allowed(client, issue):
    """Provider disclosure is a failed verdict even when every other check passes."""
    mocks = _provider_mocks(allow_cross_attempt_read=True)
    client.side_effect = [mocks["trusted_client"], mocks["capability_client"]]
    issue.return_value = _issued_capability()

    report = gcs_capability_probe.probe_attempt_gcs_runtime_capability(
        bucket_name="validation",
        project_id="validibot-project",
    )

    assert report.passed is False
    cross_read = next(
        check for check in report.checks if check.name == "cross_attempt_read"
    )
    assert cross_read.expected == "forbidden"
    assert cross_read.observed == "allowed"
    assert mocks["trusted_bucket"].list_blobs.called


@patch.object(gcs_capability_probe, "issue_attempt_gcs_runtime_capability")
@patch.object(gcs_capability_probe.storage, "Client")
def test_probe_attempts_cleanup_when_token_issuance_fails(client, issue):
    """A failed STS exchange cannot bypass cleanup of trusted control objects."""
    mocks = _provider_mocks()
    client.return_value = mocks["trusted_client"]
    issue.side_effect = RuntimeError("provider unavailable")

    with pytest.raises(
        gcs_capability_probe.GCSCapabilityProbeError,
        match=r"setup failed \(RuntimeError\); cleanup=complete",
    ):
        gcs_capability_probe.probe_attempt_gcs_runtime_capability(
            bucket_name="validation",
            project_id="validibot-project",
        )

    for cleanup_blob in mocks["cleanup_blobs"]:
        cleanup_blob.delete.assert_called_once()


@patch.object(gcs_capability_probe, "issue_attempt_gcs_runtime_capability")
@patch.object(gcs_capability_probe.storage, "Client")
def test_probe_fails_when_trusted_cleanup_is_incomplete(client, issue):
    """Successful permission checks cannot conceal residual probe objects."""
    mocks = _provider_mocks()
    client.side_effect = [mocks["trusted_client"], mocks["capability_client"]]
    issue.return_value = _issued_capability()
    mocks["cleanup_blobs"][0].delete.side_effect = RuntimeError("cleanup failed")

    report = gcs_capability_probe.probe_attempt_gcs_runtime_capability(
        bucket_name="validation",
        project_id="validibot-project",
    )

    assert report.passed is False
    cleanup = report.checks[-1]
    assert cleanup.name == "trusted_cleanup"
    assert cleanup.observed == "RuntimeError"


class TestProbeValidatorGCSCapabilityCommand(SimpleTestCase):
    """Verify the management command's configuration and automation contract."""

    @override_settings(
        GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=True,
        GCS_VALIDATION_BUCKET="validation",
        GCP_PROJECT_ID="validibot-project",
    )
    @patch(
        "validibot.validations.management.commands."
        "probe_validator_gcs_capability.probe_attempt_gcs_runtime_capability"
    )
    def test_json_success_is_stable_and_credential_free(self, probe):
        """Automation receives the versioned report without secret material."""
        probe.return_value = GCSCapabilityProbeReport(
            checks=(
                GCSCapabilityProbeCheck(
                    name="allowed_generation_read",
                    passed=True,
                    expected="allowed",
                    observed="allowed",
                ),
            )
        )
        stdout = StringIO()

        call_command("probe_validator_gcs_capability", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_version"], "validibot.gcs-capability-probe.v1")
        self.assertTrue(payload["passed"])
        self.assertNotIn("access_token", payload)
        probe.assert_called_once_with(
            bucket_name="validation",
            project_id="validibot-project",
        )

    @override_settings(GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=False)
    @patch(
        "validibot.validations.management.commands."
        "probe_validator_gcs_capability.probe_attempt_gcs_runtime_capability"
    )
    def test_disabled_capability_path_refuses_to_probe(self, probe):
        """A probe cannot certify a deployment whose actual path is disabled."""
        with self.assertRaisesMessage(
            CommandError,
            "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED must be true",
        ):
            call_command("probe_validator_gcs_capability")

        probe.assert_not_called()

    @override_settings(
        GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=True,
        GCS_VALIDATION_BUCKET="validation",
        GCP_PROJECT_ID="validibot-project",
    )
    @patch(
        "validibot.validations.management.commands."
        "probe_validator_gcs_capability.probe_attempt_gcs_runtime_capability"
    )
    def test_failed_provider_verdict_exits_nonzero(self, probe):
        """An unexpected provider permission stops rollout automation."""
        probe.return_value = GCSCapabilityProbeReport(
            checks=(
                GCSCapabilityProbeCheck(
                    name="cross_attempt_read",
                    passed=False,
                    expected="forbidden",
                    observed="allowed",
                ),
            )
        )

        with self.assertRaisesMessage(
            CommandError,
            "Attempt-scoped GCS capability probe failed",
        ):
            call_command("probe_validator_gcs_capability", stdout=StringIO())
