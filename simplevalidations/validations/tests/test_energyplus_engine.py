from __future__ import annotations

from pathlib import Path

import pytest
from sv_shared.energyplus.models import EnergyPlusSimulationMetrics
from sv_shared.energyplus.models import EnergyPlusSimulationOutputs
from sv_shared.energyplus.models import EnergyPlusSimulationResult

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
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
    assert result.stats["metrics"]["energy_use_intensity_kwh_m2"] == 18.5
    assert fake_runner.calls and fake_runner.calls[0]["return_logs"] is True


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
    assert fake_runner.calls and fake_runner.calls[0]["return_logs"] is True
