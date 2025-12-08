from __future__ import annotations

import pytest

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.energyplus import EnergyPlusValidationEngine
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


def _energyplus_ruleset():
    return RulesetFactory(
        ruleset_type=RulesetType.ENERGYPLUS,
        metadata={"weather_file": "USA_CA_SF.epw"},
        rules_text="{}",
    )


def test_energyplus_engine_returns_not_implemented():
    """
    Test that the EnergyPlus engine returns error when validate() called.

    The engine now requires validate_with_run() for Cloud Run Jobs integration.
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
        "validate_with_run" in issue.message.lower()
        and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Requires validate_with_run()"
