from __future__ import annotations

import pytest

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.energyplus import EnergyPlusValidationEngine
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


def _energyplus_ruleset():
    return RulesetFactory(
        ruleset_type=RulesetType.ENERGYPLUS,
        metadata={"weather_file": "USA_CA_SF.epw"},
        rules_text="{}",
    )


def test_energyplus_engine_returns_not_implemented():
    """
    Test that the EnergyPlus engine returns a not-implemented error.

    TODO: Phase 4 - Replace with real Cloud Run Jobs tests.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    engine = EnergyPlusValidationEngine(config={})

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
    )

    assert result.passed is False
    assert any(
        "not yet implemented" in issue.message.lower()
        and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Phase 4 - Not yet implemented"
