"""
Tests for run-aware validator execution and downstream signal/output exposure.

This test suite verifies the CEL namespace contract for cross-step data
access: validator outputs from completed steps are accessible in downstream
CEL expressions via ``steps.<step_key>.output.<name>``, and workflow-level
signals are accessible via ``s.<name>`` (or ``signal.<name>``).

The persisted run summary uses the schema:
``validation_run.summary["steps"][step_key]["output"] = {name: value}``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from validibot.actions.protocols import RunContext
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator


@pytest.mark.django_db
def test_build_cel_context_exposes_downstream_outputs():
    """Validator outputs from prior steps should be exposed under
    ``steps.<step_key>.output.<name>`` in the CEL context.

    This is the canonical cross-step reference path. The persisted
    run summary stores outputs under the ``output`` key (not ``signals``),
    and ``_extract_downstream_signals()`` reads them into the structure
    that ``_build_cel_context()`` injects as ``context["steps"]``.
    """
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidator()

    # Persisted run summary uses the output key (not signals)
    mock_validation_run = SimpleNamespace(
        summary={"steps": {"10": {"output": {"output_temp": 18.5}}}},
    )

    engine.run_context = RunContext(
        validation_run=mock_validation_run,
        step=None,
        downstream_signals={},
    )

    context = engine._build_cel_context({"input": 1}, validator)

    assert context["payload"] == {"input": 1}
    assert context["steps"]["10"]["output"]["output_temp"] == 18.5  # noqa: PLR2004


@pytest.mark.django_db
def test_build_cel_context_exposes_workflow_signals():
    """Workflow-level signals from ``RunContext.workflow_signals`` should
    appear in the CEL context under the ``s`` namespace.

    These are author-defined named values resolved from the workflow's
    signal mapping configuration before any step runs. They are available
    in CEL expressions as ``s.<name>`` or ``signal.<name>``.
    """
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidator()

    mock_validation_run = SimpleNamespace(summary={})

    engine.run_context = RunContext(
        validation_run=mock_validation_run,
        step=None,
        downstream_signals={},
        workflow_signals={"emissivity": 0.85, "panel_area": 50.0},
    )

    context = engine._build_cel_context({"input": 1}, validator)

    # Workflow signals are in the s namespace (and signal alias)
    assert context["s"]["emissivity"] == 0.85  # noqa: PLR2004
    assert context["s"]["panel_area"] == 50.0  # noqa: PLR2004
    assert context["signal"] is context["s"]


@pytest.mark.django_db
def test_run_context_workflow_signals_defaults_to_empty():
    """RunContext.workflow_signals should default to an empty dict
    when not explicitly provided. This ensures backward compatibility
    with existing code that creates RunContext without the field.
    """
    rc = RunContext(validation_run=None, step=None)
    assert rc.workflow_signals == {}
