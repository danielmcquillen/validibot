from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.actions.protocols import RunContext
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.energyplus import EnergyPlusValidationEngine
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


def _energyplus_ruleset():
    """Create a minimal EnergyPlus ruleset for testing.

    Note: weather_file is now stored in step.config, not ruleset.metadata.
    """
    return RulesetFactory(
        ruleset_type=RulesetType.ENERGYPLUS,
        rules_text="{}",
    )


def test_energyplus_engine_requires_run_context():
    """
    Test that the EnergyPlus engine returns error when run_context is not provided.

    The engine requires run_context with validation_run and step to be passed
    to validate(). This is normally done by the handler.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    engine = EnergyPlusValidationEngine(config={})

    # Don't pass run_context - should fail
    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
        run_context=None,
    )

    assert result.passed is False
    assert any(
        "workflow context" in issue.message.lower()
        and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Missing run_context"


def test_energyplus_engine_backend_not_available():
    """
    Test that the EnergyPlus engine returns error when execution backend not available.

    When run_context is provided but the execution backend (Docker or Cloud Run)
    is not available, the engine should return a helpful error.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    engine = EnergyPlusValidationEngine(config={})

    # Create run_context with mock objects
    run_context = RunContext(
        validation_run=MagicMock(id=1),
        step=MagicMock(id=1),
        downstream_signals={},
    )

    # Mock the backend to be unavailable
    clear_backend_cache()
    with patch(
        "validibot.validations.services.execution.get_execution_backend"
    ) as mock_get_backend:
        mock_backend = MagicMock()
        mock_backend.is_available.return_value = False
        mock_backend.backend_name = "MockBackend"
        mock_get_backend.return_value = mock_backend

        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            run_context=run_context,
        )

    assert result.passed is False
    assert any(
        "not available" in issue.message.lower()
        and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Backend not available"
