"""Tests for deployment-pinned callback runtime authorization.

The site-wide worker allowlist authenticates infrastructure callers, while the
attempt snapshot authorizes the one runtime identity selected for a managed
execution. These focused tests ensure a valid attempt nonce cannot be replayed
by a different otherwise-allowlisted service account.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status

from validibot.validations.services.execution.gcp_job_import import GCPJobObservation
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    build_callback_nonce_verifier,
)
from validibot.validations.services.execution_attempts import (
    get_or_create_execution_attempt,
)
from validibot.validations.services.validation_callback import ValidationCallbackService
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory

CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
EXPECTED_IDENTITY = "validator-runtime@example.iam.gserviceaccount.com"
DIGEST = "sha256:" + "f" * 64
PROJECT_ID = "test-project"


def _payload(attempt):
    """Build the minimal callback bound to the managed attempt."""
    return {
        "run_id": str(attempt.step_run.validation_run_id),
        "callback_id": build_attempt_callback_id(attempt),
        "callback_nonce": CALLBACK_NONCE,
        "status": "success",
        "result_uri": "gs://bucket/output.json",
    }


def _managed_attempt():
    """Allocate an attempt through a real ready deployment route."""
    validator = ValidatorFactory()
    step_run = ValidationStepRunFactory(workflow_step__validator=validator)
    observation = GCPJobObservation(
        resource_name=(
            f"projects/{PROJECT_ID}/locations/australia-southeast1/jobs/validator"
        ),
        job_name="validator",
        revision="0.14.0",
        image_ref=f"example.invalid/validator@{DIGEST}",
        image_digest=DIGEST,
        runtime_service_account=EXPECTED_IDENTITY,
        maximum_execution_seconds=1500,
        maximum_cpu_millis=1000,
        maximum_memory_mib=1024,
    )
    register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region="australia-southeast1",
        observation=observation,
        activate_primary=True,
    )
    attempt, _ = get_or_create_execution_attempt(
        step_run,
        validator=validator,
        managed=True,
        effective_budget_seconds=900,
    )
    attempt.callback_nonce_hash = build_callback_nonce_verifier(CALLBACK_NONCE)
    attempt.save(update_fields=["callback_nonce_hash"])
    return attempt


@pytest.mark.django_db
def test_managed_callback_rejects_a_different_allowlisted_runtime_identity():
    """OIDC authentication alone must not authorize another deployment's caller."""
    attempt = _managed_attempt()

    response = ValidationCallbackService().process(
        payload=_payload(attempt),
        caller_email="other-runtime@example.iam.gserviceaccount.com",
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.data["error"] == (
        "Callback runtime identity does not match attempt"
    )


@pytest.mark.django_db
@patch.object(ValidationCallbackService, "_process_callback")
def test_managed_callback_accepts_the_exact_snapshot_runtime_identity(
    process_callback,
):
    """The selected deployment identity may proceed after its nonce also matches."""
    process_callback.return_value = SimpleNamespace(status_code=status.HTTP_200_OK)
    attempt = _managed_attempt()

    response = ValidationCallbackService().process(
        payload=_payload(attempt),
        caller_email=EXPECTED_IDENTITY.upper(),
    )

    assert response.status_code == status.HTTP_200_OK
    process_callback.assert_called_once()
