"""
Tests for the custom (user-defined) validator.

Custom validators are container-based, so they follow the same pattern as
EnergyPlus and FMU: they require run_context and an execution backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.actions.protocols import RunContext
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base.registry import get
from validibot.validations.validators.custom.validator import CustomValidator

pytestmark = pytest.mark.django_db


def test_custom_validator_is_registered():
    """CustomValidator should be retrievable from the registry."""
    cls = get(ValidationType.CUSTOM_VALIDATOR)
    assert cls is CustomValidator


def test_custom_validator_requires_run_context():
    """Custom validator returns error when run_context is not provided."""
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.BASIC,
        rules_text="{}",
    )
    submission = SubmissionFactory(content='{"test": "data"}')

    engine = CustomValidator(config={})

    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
        run_context=None,
    )

    assert result.passed is False
    assert any(
        "workflow context" in issue.message.lower() and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats["implementation_status"] == "Missing run_context"


def test_custom_validator_backend_not_available():
    """Custom validator returns error when execution backend is unavailable."""
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.BASIC,
        rules_text="{}",
    )
    submission = SubmissionFactory(content='{"test": "data"}')

    engine = CustomValidator(config={})

    run_context = RunContext(
        validation_run=MagicMock(id=1),
        step=MagicMock(id=1),
        downstream_signals={},
    )

    clear_backend_cache()
    with patch(
        "validibot.validations.services.execution.get_execution_backend",
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
        "not available" in issue.message.lower() and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats["implementation_status"] == "Backend not available"


def test_extract_output_signals_from_dict():
    """extract_output_signals extracts signals from outputs.signals dict."""
    from types import SimpleNamespace

    envelope = SimpleNamespace(
        outputs=SimpleNamespace(
            signals={"temperature": 21.5, "humidity": 60},
        ),
    )

    result = CustomValidator.extract_output_signals(envelope)

    assert result == {"temperature": 21.5, "humidity": 60}


def test_extract_output_signals_returns_none_without_outputs():
    """extract_output_signals returns None when envelope has no outputs."""
    envelope = MagicMock(spec=[])  # no attributes

    result = CustomValidator.extract_output_signals(envelope)

    assert result is None
