"""
Tests for run-aware validator execution and downstream signal exposure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from validibot.actions.protocols import RunContext
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator


@pytest.mark.django_db
def test_build_cel_context_exposes_downstream_signals():
    """CEL context should include signals from prior steps under steps.<id>.signals."""
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidator()

    # Create a mock validation_run with a summary containing step signals
    mock_validation_run = SimpleNamespace(
        summary={"steps": {"10": {"signals": {"output_temp": 18.5}}}},
    )

    # Set run_context on the validator (this is normally done during validate())
    engine.run_context = RunContext(
        validation_run=mock_validation_run,
        step=None,
        downstream_signals={},
    )

    context = engine._build_cel_context({"input": 1}, validator)

    assert context["payload"] == {"input": 1}
    assert context["steps"]["10"]["signals"]["output_temp"] == 18.5  # noqa: PLR2004
