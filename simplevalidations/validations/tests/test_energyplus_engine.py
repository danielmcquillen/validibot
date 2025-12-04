from __future__ import annotations

from pathlib import Path

import pytest
from sv_shared.energyplus.models import EnergyPlusSimulationMetrics
from sv_shared.energyplus.models import EnergyPlusSimulationOutputs

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.energyplus import EnergyPlusSimulationResult
from simplevalidations.validations.engines.energyplus import EnergyPlusValidationEngine
from simplevalidations.validations.engines.energyplus import configure_modal_runner
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


class FakeRunner:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeCleanupRunner:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture(autouse=True)
def reset_modal_runner():
    configure_modal_runner(None)
    yield
    configure_modal_runner(None)


def _energyplus_ruleset():
    return RulesetFactory(
        ruleset_type=RulesetType.ENERGYPLUS,
        metadata={"weather_file": "USA_CA_SF.epw"},
        rules_text="{}",
    )


def test_energyplus_engine_success_path():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-123",
        status="success",
        outputs=EnergyPlusSimulationOutputs(
            eplusout_sql=Path("outputs/sim-123/eplusout.sql"),
        ),
        metrics=EnergyPlusSimulationMetrics(
            electricity_kwh=1200.0,
            energy_use_intensity_kwh_m2=18.5,
        ),
        messages=["Simulation completed."],
        errors=[],
        energyplus_returncode=0,
        execution_seconds=42.0,
        invocation_mode="python_api",
        energyplus_input_file_path=Path("inputs/sim-123.epJSON"),
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
    configure_modal_runner(fake_runner)

    engine = EnergyPlusValidationEngine(
        config={
            "simulation_checks": ["eui-range"],
            "eui_band": {"min": 10, "max": 20},
        },
    )

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is True
    assert all(issue.severity != Severity.ERROR for issue in result.issues)
    assert result.stats is not None
    assert result.stats["simulation_id"] == "sim-123"
    assert result.stats["metrics"]["energy_use_intensity_kwh_m2"] == 18.5 # noqa: PLR2004
    assert result.stats["energyplus_input_file_path"] == "inputs/sim-123.epJSON"
    assert fake_runner.calls
    first_call = fake_runner.calls[0]
    assert first_call["return_logs"] is True
    assert "energyplus_payload" in first_call


def test_energyplus_engine_surfaces_modal_errors():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Failure Case"}')

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-999",
        status="error",
        outputs=EnergyPlusSimulationOutputs(),
        metrics=EnergyPlusSimulationMetrics(),
        messages=[],
        errors=["EnergyPlus failed to converge."],
        energyplus_returncode=1,
        execution_seconds=12.0,
        invocation_mode="python_api",
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
    configure_modal_runner(fake_runner)

    engine = EnergyPlusValidationEngine(config={})
    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is False
    assert any(
        "EnergyPlus failed to converge." in issue.message for issue in result.issues
    )
    assert fake_runner.calls
    first_call = fake_runner.calls[0]
    assert first_call["return_logs"] is True
    assert "energyplus_payload" in first_call


def test_energyplus_engine_accepts_idf_payload():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    idf_payload = "Version, 23.1;\nBuilding, Example;"
    submission = SubmissionFactory(content=idf_payload)

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-idf",
        status="success",
        outputs=EnergyPlusSimulationOutputs(),
        metrics=EnergyPlusSimulationMetrics(),
        messages=["Simulation completed."],
        errors=[],
        energyplus_returncode=0,
        execution_seconds=8.0,
        invocation_mode="cli",
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
    configure_modal_runner(fake_runner)

    engine = EnergyPlusValidationEngine(config={})
    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is True
    assert fake_runner.calls
    first_call = fake_runner.calls[0]
    assert first_call["energyplus_payload"] == idf_payload


def test_energyplus_engine_runs_cleanup_when_configured():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-clean",
        status="success",
        outputs=EnergyPlusSimulationOutputs(
            eplusout_sql=Path("outputs/sim-clean/eplusout.sql"),
        ),
        metrics=EnergyPlusSimulationMetrics(
            electricity_kwh=500.0,
        ),
        errors=[],
        energyplus_returncode=0,
        execution_seconds=10.0,
        invocation_mode="cli",
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
    fake_cleanup = FakeCleanupRunner({"simulation_id": "sim-clean", "deleted": True})

    configure_modal_runner(fake_runner, cleanup_callable=fake_cleanup)

    engine = EnergyPlusValidationEngine(
        config={
            "cleanup_after_run": True,
            "cleanup_missing_ok": False,
        },
    )

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is True
    assert fake_cleanup.calls
    cleanup_call = fake_cleanup.calls[0]
    assert cleanup_call["simulation_id"] == "sim-clean"
    assert cleanup_call["missing_ok"] is False
    assert result.stats is not None
    assert result.stats["cleanup_requested"] is True
    assert result.stats["cleanup_result"]["deleted"] is True


def test_energyplus_engine_cleanup_failure_adds_warning():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-clean",
        status="success",
        outputs=EnergyPlusSimulationOutputs(),
        metrics=EnergyPlusSimulationMetrics(),
        errors=[],
        energyplus_returncode=0,
        execution_seconds=5.0,
        invocation_mode="cli",
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))

    class RaisingCleanup:
        def call(self, **_kwargs):
            raise RuntimeError("cleanup explosion")

    configure_modal_runner(fake_runner, cleanup_callable=RaisingCleanup())

    engine = EnergyPlusValidationEngine(
        config={
            "cleanup_after_run": True,
        },
    )

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is True
    assert any(
        "EnergyPlus cleanup failed" in issue.message
        and issue.severity == Severity.WARNING
        for issue in result.issues
    )
    assert result.stats is not None
    assert "cleanup_error" in result.stats


def test_energyplus_engine_skips_cleanup_by_default():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    simulation_result = EnergyPlusSimulationResult(
        simulation_id="sim-clean",
        status="success",
        outputs=EnergyPlusSimulationOutputs(),
        metrics=EnergyPlusSimulationMetrics(),
        errors=[],
        energyplus_returncode=0,
        execution_seconds=7.0,
        invocation_mode="cli",
    )

    fake_runner = FakeRunner(simulation_result.model_dump(mode="json"))
    fake_cleanup = FakeCleanupRunner({"simulation_id": "sim-clean", "deleted": True})

    configure_modal_runner(fake_runner, cleanup_callable=fake_cleanup)

    engine = EnergyPlusValidationEngine(config={})

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is True
    assert not fake_cleanup.calls
    assert result.stats is not None
    assert "cleanup_requested" not in result.stats
