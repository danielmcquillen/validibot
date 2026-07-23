"""Tests for the one-command managed-validator acceptance feature.

The suite protects the operator-facing simplicity as well as the safety
contract behind it: reports must fail closed, live route checks must pin the
requested release, measurements must not blend revisions, production assets
must actually ship, and the GCP recipe must always restore maintenance mode.
Provider-specific HTTP and immutable-I/O behavior remains covered by its
lower-level conformance suites; these tests cover their acceptance orchestration.
"""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.acceptance import BACKENDS
from validibot.validations.acceptance import AcceptanceFixtureBuilder
from validibot.validations.acceptance import AcceptanceReport
from validibot.validations.acceptance import AcceptanceScenario
from validibot.validations.acceptance import ValidatorAcceptanceRunner
from validibot.validations.acceptance import _percentile
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ValidationRunStatus

EXPECTED_SINGLE_SAMPLE_P95 = 3.0
EXPECTED_TWENTY_SAMPLE_P50 = 10.0
EXPECTED_TWENTY_SAMPLE_P95 = 19.0
EXPECTED_ENERGYPLUS_U_FACTOR = 2.0
EXPECTED_FMU_INPUT = 42.0
REPRESENTATIVE_SAMPLE_SIZE = 20
SHA256_HEX_LENGTH = 64


@override_settings(VALIDIBOT_STAGE="staging")
def test_report_is_machine_readable_and_fails_closed():
    """One failed check must make the retained top-level verdict false."""
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=20)
    report.add("VA-OK", "passed", "A prerequisite passed.")
    report.add("VA-NO", "failed", "A canary failed.", error="bounded")
    report.finish()

    document = report.as_dict()

    assert document["schema_version"] == "validibot.validator-acceptance.v1"
    assert document["stage"] == "staging"
    assert document["backend_release"] == "v1.2.3"
    assert document["passed"] is False
    assert [check["id"] for check in document["checks"]] == ["VA-OK", "VA-NO"]
    json.dumps(document)


def test_nearest_rank_percentile_is_deterministic_for_small_and_full_bursts():
    """Stable percentile math keeps repeated reports directly comparable."""
    assert _percentile([3.0], 95) == EXPECTED_SINGLE_SAMPLE_P95
    assert (
        _percentile([float(value) for value in range(1, 21)], 50)
        == EXPECTED_TWENTY_SAMPLE_P50
    )
    assert (
        _percentile([float(value) for value in range(1, 21)], 95)
        == EXPECTED_TWENTY_SAMPLE_P95
    )


@pytest.mark.parametrize("release_tag", ["1.2.3", "latest", "v1.2"])
def test_runner_rejects_mutable_or_malformed_release_identity(release_tag):
    """Acceptance evidence is meaningless unless it names an immutable release."""
    with pytest.raises(ValueError, match=r"vX\.Y\.Z"):
        ValidatorAcceptanceRunner(release_tag=release_tag)


@pytest.mark.parametrize("attempts", [0, 21])
def test_runner_bounds_operator_requested_burst_size(attempts):
    """A typo must not create an unbounded provider load or a zero-run pass."""
    with pytest.raises(ValueError, match="between 1 and 20"):
        ValidatorAcceptanceRunner(
            release_tag="v1.2.3",
            attempts_per_backend=attempts,
        )


def test_preflight_failure_stops_before_any_canary_is_created():
    """A drifted route must fail without adding workload to an unsafe release."""
    runner = ValidatorAcceptanceRunner(
        release_tag="v1.2.3",
        run_storage_probe=False,
    )
    with (
        patch.object(runner, "_check_deployments", return_value=False),
        patch.object(runner, "_check_storage") as storage_check,
        patch(
            "validibot.validations.acceptance.AcceptanceFixtureBuilder"
        ) as fixture_builder,
    ):
        report = runner.run()

    storage_check.assert_called_once()
    fixture_builder.assert_not_called()
    assert report.passed is False
    assert report.checks[-1].check_id == "VA-SMOKE-ABORTED"


def test_storage_gate_requires_operator_iam_proof():
    """A token probe alone must not conceal unverified ambient runtime IAM."""
    runner = ValidatorAcceptanceRunner(release_tag="v1.2.3")
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=20)

    with patch(
        "validibot.validations.acceptance.probe_attempt_gcs_runtime_capability"
    ) as provider_probe:
        runner._check_storage(report)

    provider_probe.assert_not_called()
    assert report.checks[-1].status == "failed"
    assert "not verified" in report.checks[-1].summary


@override_settings(
    GCS_VALIDATION_BUCKET="private-bucket",
    GCP_PROJECT_ID="validibot-test",
)
def test_operator_iam_proof_allows_storage_acceptance():
    """The offline recipe's Policy Troubleshooter proof unlocks the live probe."""
    runner = ValidatorAcceptanceRunner(
        release_tag="v1.2.3",
        ambient_isolation_verified=True,
    )
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=20)
    provider_result = SimpleNamespace(passed=True, checks=[])

    with patch(
        "validibot.validations.acceptance.probe_attempt_gcs_runtime_capability",
        return_value=provider_result,
    ):
        runner._check_storage(report)

    assert report.checks[-1].status == "passed"
    assert report.checks[-1].details["ambient_storage_access_verified"] is True


def test_route_preflight_accepts_only_requested_service_and_ready_job():
    """A green route check must prove both candidate identity and rollback path."""
    runner = ValidatorAcceptanceRunner(release_tag="v1.2.3")
    validator = SimpleNamespace(pk="validator-1")
    service = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        readiness_state=ExecutionDeploymentReadiness.READY,
        emergency_blocked=False,
        last_verification_succeeded=True,
        backend_release_identity="1.2.3",
        backend_image_digest="sha256:" + "a" * 64,
    )
    job = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        readiness_state=ExecutionDeploymentReadiness.READY,
    )
    routes = MagicMock()
    routes.filter.return_value = [service, job]
    config = SimpleNamespace(slug="shacl", version="1")

    with (
        patch("validibot.validations.acceptance.get_config", return_value=config),
        patch(
            "validibot.validations.acceptance.Validator.objects.get",
            return_value=validator,
        ),
        patch(
            "validibot.validations.acceptance.ValidatorExecutionDeployment.objects",
            routes,
        ),
    ):
        resolved = runner._accepted_routes(BACKENDS[2], "1.2.3")

    assert resolved == (validator, service, job)


def test_route_preflight_rejects_a_different_backend_release():
    """A healthy but stale Service must never be accepted for a newer release."""
    runner = ValidatorAcceptanceRunner(release_tag="v1.2.3")
    service = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        readiness_state=ExecutionDeploymentReadiness.READY,
        emergency_blocked=False,
        last_verification_succeeded=True,
        backend_release_identity="1.2.2",
        backend_image_digest="sha256:" + "a" * 64,
    )
    job = SimpleNamespace(
        routing_role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        readiness_state=ExecutionDeploymentReadiness.READY,
    )
    routes = MagicMock()
    routes.filter.return_value = [service, job]

    with (
        patch(
            "validibot.validations.acceptance.get_config",
            return_value=SimpleNamespace(slug="shacl", version="1"),
        ),
        patch(
            "validibot.validations.acceptance.Validator.objects.get",
            return_value=SimpleNamespace(pk="validator-1"),
        ),
        patch(
            "validibot.validations.acceptance.ValidatorExecutionDeployment.objects",
            routes,
        ),
        pytest.raises(ValueError, match=r"release is 1\.2\.2"),
    ):
        runner._accepted_routes(BACKENDS[2], "1.2.3")


def test_latency_gate_uses_only_attempts_from_the_exact_launched_burst():
    """Old fast samples must not hide missing or slow evidence in this release."""
    runner = ValidatorAcceptanceRunner(
        release_tag="v1.2.3",
        attempts_per_backend=2,
    )
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * 64,
    )
    accepted_at = timezone.now()
    attempts = [
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=2),
            callback_received_at=accepted_at + timedelta(seconds=5),
        ),
        SimpleNamespace(
            deployment=SimpleNamespace(
                deployment_revision="service-r7",
                minimum_instances=0,
            ),
            provider_accepted_at=accepted_at,
            provider_started_at=accepted_at + timedelta(seconds=3),
            callback_received_at=accepted_at + timedelta(seconds=6),
        ),
    ]
    querysets = []
    for attempt in attempts:
        queryset = MagicMock()
        queryset.select_related.return_value.order_by.return_value.last.return_value = (
            attempt
        )
        querysets.append(queryset)
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=2)
    launched = [
        (1, SimpleNamespace(pk="run-1")),
        (2, SimpleNamespace(pk="run-2")),
    ]

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        side_effect=querysets,
    ):
        runner._record_latency(report, scenario, launched)

    check = report.checks[-1]
    assert check.status == "passed"
    assert check.details["provider_start_p95_seconds"] == EXPECTED_SINGLE_SAMPLE_P95
    assert check.details["deployment_revisions"] == ["service-r7"]
    assert check.details["representative_sample"] is False


def test_smoke_verdict_requires_matching_immutable_attempt_provenance():
    """A successful run is not accepted when its observed image differs."""
    runner = ValidatorAcceptanceRunner(release_tag="v1.2.3")
    scenario = AcceptanceScenario(
        backend=BACKENDS[2],
        workflow=SimpleNamespace(),
        inline_text="fixture",
        filename="fixture.ttl",
        file_type=SubmissionFileType.TEXT,
        fixture_sha256="a" * SHA256_HEX_LENGTH,
    )
    image_digest = "sha256:" + "a" * SHA256_HEX_LENGTH
    deployment = SimpleNamespace(
        pk="deployment-1",
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        deployment_revision="service-r7",
        backend_release_identity="1.2.3",
        backend_image_digest=image_digest,
    )
    now = timezone.now()
    attempt = SimpleNamespace(
        pk="attempt-1",
        state="COMPLETED",
        deployment_id="deployment-1",
        deployment=deployment,
        deployment_snapshot={
            "deployment_id": "deployment-1",
            "deployment_revision": "service-r7",
            "backend_image_digest": image_digest,
        },
        backend_image_digest="sha256:" + "b" * SHA256_HEX_LENGTH,
        input_envelope_sha256="c" * SHA256_HEX_LENGTH,
        input_evidence_snapshot={"files": [{"sha256": "d" * SHA256_HEX_LENGTH}]},
        output_envelope_sha256="e" * SHA256_HEX_LENGTH,
        provider_accepted_at=now,
        provider_started_at=now + timedelta(seconds=1),
        provider_finished_at=now + timedelta(seconds=2),
        callback_received_at=now + timedelta(seconds=3),
    )
    queryset = MagicMock()
    queryset.select_related.return_value.order_by.return_value.last.return_value = (
        attempt
    )
    run = SimpleNamespace(
        pk="run-1",
        status=ValidationRunStatus.SUCCEEDED,
        error="",
    )
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=1)

    with patch(
        "validibot.validations.acceptance.ExecutionAttempt.objects.filter",
        return_value=queryset,
    ):
        runner._record_run(report, scenario, 1, run)

    assert report.checks[-1].status == "failed"
    assert report.checks[-1].details["error"] == (
        "attempt observed a different backend image"
    )


def test_submission_fixtures_are_deterministic_and_domain_representative():
    """Canaries must exercise real parser/simulation paths, not empty payloads."""
    builder = object.__new__(AcceptanceFixtureBuilder)

    energyplus_text, _, _ = builder._submission_fixture(BACKENDS[0])
    fmu_text, _, _ = builder._submission_fixture(BACKENDS[1])
    shacl_text, _, _ = builder._submission_fixture(BACKENDS[2])
    schematron_text, _, _ = builder._submission_fixture(BACKENDS[3])

    assert json.loads(energyplus_text)["U_FACTOR"] == EXPECTED_ENERGYPLUS_U_FACTOR
    assert json.loads(fmu_text)["real_continuous_in"] == EXPECTED_FMU_INPUT
    assert "ValidPerson" in shacl_text or "Person" in shacl_text
    assert "calibration" in schematron_text.lower()


@pytest.mark.django_db
def test_fixture_builder_creates_and_reuses_all_four_real_workflows():
    """A deployed command must provision complete canaries without manual setup."""
    call_command("sync_validators", stdout=StringIO(), stderr=StringIO())
    call_command("seed_weather_files", stdout=StringIO(), stderr=StringIO())

    first = AcceptanceFixtureBuilder().build_all()
    second = AcceptanceFixtureBuilder().build_all()

    assert set(first) == {backend.key for backend in BACKENDS}
    assert {key: scenario.workflow.pk for key, scenario in first.items()} == {
        key: scenario.workflow.pk for key, scenario in second.items()
    }
    assert all(scenario.workflow.steps.count() == 1 for scenario in first.values())
    assert all(
        len(scenario.fixture_sha256) == SHA256_HEX_LENGTH for scenario in first.values()
    )


def test_runtime_image_includes_only_explicit_acceptance_assets():
    """Production must have canaries without accidentally shipping all tests."""
    dockerignore = (Path(settings.BASE_DIR) / ".dockerignore").read_text()

    assert "tests/*" in dockerignore
    assert "!tests/assets/fmu/Feedthrough.fmu" in dockerignore
    assert "!tests/assets/idf/window_glazing_template.idf" in dockerignore
    assert "!tests/assets/shacl/valid_person.ttl" in dockerignore
    assert (
        "!tests/assets/schematron/calibration/calibration-rules-demo.sch"
        in dockerignore
    )


def test_management_command_persists_and_prints_one_json_result():
    """Automation needs one parseable report location, not copied console notes."""
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=20)
    report.add("VA-ALL", "passed", "All checks passed.")
    report.finish()
    output = StringIO()

    with (
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "ValidatorAcceptanceRunner"
        ) as runner_class,
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "persist_acceptance_report",
            return_value={"uri": "gs://private/report.json", "sha256": "a" * 64},
        ),
    ):
        runner_class.return_value.run.return_value = report
        call_command(
            "run_validator_acceptance",
            release_tag="v1.2.3",
            require_persisted_report=True,
            stdout=output,
        )

    document = json.loads(output.getvalue())
    assert document["passed"] is True
    assert document["attempts_per_backend"] == REPRESENTATIVE_SAMPLE_SIZE
    assert document["evidence"]["uri"] == "gs://private/report.json"


def test_management_command_fails_when_private_evidence_is_not_configured():
    """Production automation must not turn an unretained console pass into proof."""
    report = AcceptanceReport(release_tag="v1.2.3", attempts_per_backend=20)
    report.add("VA-ALL", "passed", "All checks passed.")
    report.finish()

    with (
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "ValidatorAcceptanceRunner"
        ) as runner_class,
        patch(
            "validibot.validations.management.commands.run_validator_acceptance."
            "persist_acceptance_report",
            return_value=None,
        ),
    ):
        runner_class.return_value.run.return_value = report
        with pytest.raises(CommandError, match="persistence is not configured"):
            call_command(
                "run_validator_acceptance",
                release_tag="v1.2.3",
                require_persisted_report=True,
                stdout=StringIO(),
            )


def test_gcp_recipe_is_one_command_with_automatic_safety_cleanup():
    """The operator path must restore maintenance and rollback any failed release."""
    recipe = (Path(settings.BASE_DIR) / "just" / "gcp" / "mod.just").read_text()
    start = recipe.index("validator-acceptance stage release_tag")
    end = recipe.index("# Reconcile the validator dashboard", start)
    acceptance_recipe = recipe[start:end]

    assert "_maintenance-assert-offline" in acceptance_recipe
    assert "trap restore_acceptance_state EXIT" in acceptance_recipe
    assert "validator-services-rollback" in acceptance_recipe
    assert "validator-services-activate" in acceptance_recipe
    assert "gcloud tasks list" in acceptance_recipe
    assert "gcloud tasks tasks list" not in acceptance_recipe
    assert "assert_release_resources_exist" in acceptance_recipe
    assert "gcloud run jobs describe" in acceptance_recipe
    assert (
        "just gcp validator-deploy-all {{stage}} {{release_tag}}" in acceptance_recipe
    )
    assert "validators-deploy-all" not in acceptance_recipe
    assert "sync_gcp_validator_deployments --activate-primary" in acceptance_recipe
    assert "validator-storage-isolation" in acceptance_recipe
    assert "restoring the legacy run-prefix storage binding" not in acceptance_recipe
    assert "--require-persisted-report" in acceptance_recipe
    assert "--ambient-isolation-verified" in acceptance_recipe
    assert "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED" not in acceptance_recipe
    assert (
        "GCS_VALIDATOR_RUNTIME_IDENTITY_STORAGE_ACCESS_DISABLED"
        not in acceptance_recipe
    )
    assert "production acceptance requires exactly 20" in acceptance_recipe
    assert "maintenance-off" not in acceptance_recipe
